# AgroClima — Backend Python (API analítica)

API em **FastAPI** que entrega os indicadores do AgroClima: temperatura e umidade (ThingSpeak), dashboards, qualidade dos dados e comparações.

O Python **não acessa o banco nem o ThingSpeak**. Quem faz isso é a **API Java**, que também valida login e permissões. O Python busca os dados nela, calcula as análises e devolve para o frontend.

```
Frontend (Vite, :5173)  ──►  API Python (:8000)  ──►  API Java (:8080)  ──►  MySQL / ThingSpeak
```

## Requisitos

- Python 3.11 ou mais novo
- API Java rodando na porta 8080 (com o MySQL e o banco `agroclima`)

## Como rodar

Entre na pasta que contém o `requirements.txt` e execute:

**1. Crie o `.env`**

```powershell
Copy-Item .env.example .env
```

No CMD, use `copy .env.example .env`.

Preencha o `INTERNAL_API_KEY` com o **mesmo valor** do `.env` da API Java (mínimo 32 caracteres). Para gerar um:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

**2. Instale as dependências**

```powershell
pip install -r requirements-dev.txt
```

**3. Suba a API**

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Use `python -m ...` em vez do comando solto: no Windows, o `uvicorn` (e o `ruff`, `mypy`) podem não estar no PATH.

**4. Confira se está funcionando**

| O quê | Como |
|---|---|
| Documentação (Swagger) | http://localhost:8000/docs |
| API de pé | `curl.exe -i http://localhost:8000/health` |
| Pronta de verdade | `curl.exe -i http://localhost:8000/ready` |

O `/ready` deve responder `200` com `"status":"READY"`. Se responder `503`, veja o campo `checks`: `javaApi: false` indica API Java fora do ar, e `internalKey: false` indica chave interna ausente ou com menos de 32 caracteres.

## Rodando o sistema completo

Suba as três partes, cada uma em um terminal:

1. **API Java** na porta 8080, com o MySQL rodando e o banco criado: `CREATE DATABASE IF NOT EXISTS agroclima CHARACTER SET utf8mb4;`
2. **API Python** (passos acima) na porta 8000.
3. **Frontend**, na pasta dele:
   ```powershell
   copy .env.example .env
   npm install
   npm run dev
   ```
   Depois, abra http://localhost:5173.

## Endpoints

Todas as rotas `/api/v1/*` exigem `Authorization: Bearer <token>`. O token é validado na API Java.

| Método | Rota | Quem acessa |
|---|---|---|
| GET | `/api/v1/climate/current` | Qualquer usuário logado (últimas 24 h) |
| GET | `/api/v1/climate/history` | Qualquer usuário logado (`start`, `end`, `resolution`) |
| GET | `/api/v1/dashboard/summary` | Qualquer usuário logado |
| GET | `/api/v1/dashboard/producer` | Produtor/Admin (**obsoleta**, use `/summary`) |
| GET | `/api/v1/dashboard/analyst` | Analista/Admin |
| GET | `/api/v1/dashboard/quality` | Analista/Admin |
| GET | `/api/v1/dashboard/comparison` | Analista/Admin |
| GET | `/api/v1/dashboard/market` | Qualquer usuário logado |
| GET | `/api/v1/dashboard/forecast` | Qualquer usuário logado |
| GET | `/api/v1/integrations/thingspeak/status` | Admin |
| POST | `/api/v1/integrations/thingspeak/sync` | Admin (`from` e `to` opcionais) |
| GET | `/health` | Público (liveness) |
| GET | `/ready` | Público (readiness) |

**Resolução do histórico** (`resolution`): `auto` usa leituras brutas até 48 h, por hora até 31 dias e por dia acima disso. `raw` é recusado se o período passar do limite de pontos. Os valores `hour` e `day` agregam a série. As métricas (média, mínimo, máximo) sempre usam as leituras brutas.

## Configuração (`.env`)

| Variável | Padrão | Para quê |
|---|---|---|
| `JAVA_API_URL` | `http://localhost:8080` | Endereço da API Java |
| `INTERNAL_API_KEY` | (vazio) | Chave de serviço, igual à do Java (mín. 32 caracteres) |
| `APP_ENV` | `development` | `production` exige `CORS_ALLOWED_ORIGINS` explícito |
| `CORS_ALLOWED_ORIGINS` | (vazio) | Origens do frontend, separadas por vírgula. Não aceita `*` |
| `CORS_ALLOW_CREDENTIALS` | `false` | A API usa Bearer token, não cookies |
| `REQUEST_TIMEOUT_SECONDS` | `12` | Tempo máximo de cada chamada ao Java (1 a 60) |
| `JAVA_RETRY_ATTEMPTS` | `3` | Total de tentativas nas consultas ao Java (1 a 5; 1 desliga o retry) |
| `JAVA_RETRY_BACKOFF_SECONDS` | `0.25` | Espera inicial entre tentativas (dobra a cada uma) |
| `PROFILE_CACHE_TTL_SECONDS` | `45` | Cache do perfil do usuário (0 desliga; máx. 300) |
| `HISTORY_MAX_RAW_POINTS` | `5000` | Limite de pontos brutos no histórico |

> **Nunca suba o `.env` para o GitHub.** Ele já está no `.gitignore`. Use o `.env.example` como modelo.

## Confiabilidade e logs

- **Retry com backoff**: consultas (GET) ao Java são repetidas em caso de falha de conexão, timeout ou 502/503/504. A sincronização (POST) **nunca** é repetida, para não rodar duas vezes.
- **`X-Request-ID`**: cada requisição recebe um id único, devolvido no header da resposta, no campo `requestId` dos erros e em todas as linhas de log (`[request_id]`). Se um usuário reportar um erro, é só procurar o id no log.
- **Erros 500**: o traceback completo vai para o log (`logger.exception`), e o cliente vê só uma mensagem genérica.
- **`/health` vs `/ready`**: `/health` só diz que o processo está de pé. `/ready` confere a chave interna e se a API Java responde (`/actuator/health`, e qualquer resposta abaixo de 500 conta como "no ar").

## Testes e qualidade

```powershell
python -m ruff check .   # estilo e erros comuns
python -m mypy           # tipos
python -m pytest -q      # testes
```

O **CI** (`.github/workflows/ci.yml`) roda os três a cada push na `main` e em todo Pull Request, no Python 3.11 e 3.12.

## Docker

```powershell
docker build -t agroclima-python .
docker run -p 8000:8000 --env-file .env agroclima-python
```

Dentro do container, `localhost` é o próprio container. Para alcançar a API Java na sua máquina, use `JAVA_API_URL=http://host.docker.internal:8080` no `.env`.

## Estrutura

```
app/
  main.py              # criação da app, middlewares, /health e /ready
  config.py            # configurações (.env)
  models.py            # modelos de resposta e enum PlantationStatus
  errors.py            # formato padrão de erro
  logging_config.py    # request_id e formato dos logs
  cache.py             # cache curto do perfil
  api/routes.py        # rotas (finas: só chamam o service)
  services/
    dashboard.py       # regras dos dashboards
    analysis.py        # cálculos (pandas/numpy)
  clients/java_api.py  # único ponto de acesso à API Java (com retry)
tests/                 # pytest
pyproject.toml         # configuração de ruff, mypy e pytest
.github/workflows/     # CI
```

## Contribuindo

1. Crie uma branch a partir da `main`.
2. Antes de abrir o Pull Request, rode `ruff`, `mypy` e `pytest`. O CI roda os mesmos três.
3. Não commite o `.env`.
