# AgroClima — Backend Python (API analítica)

API em **FastAPI** que entrega os indicadores do AgroClima: temperatura e umidade (ThingSpeak), dashboards, qualidade dos dados, comparações e, para a exportação de uvas, **graus-dia, risco de míldio/oídio, janela de colheita, alertas e previsão**.

O Python **não acessa o banco nem o ThingSpeak**. Quem faz isso é a **API Java**, que também valida login e permissões. O Python busca os dados nela, calcula as análises e devolve para o frontend.

```
Frontend (Vite, :5173)  ──►  API Python (:8000)  ──►  API Java (:8080)  ──►  MySQL / ThingSpeak
```

## Sumário

[Requisitos](#requisitos) · [Como rodar](#como-rodar) · [Sistema completo](#rodando-o-sistema-completo) · [Endpoints](#endpoints) · [Indicadores de exportação](#indicadores-de-exportação) · [Alertas e previsão](#alertas-e-previsão) · [Configuração](#configuração-env) · [Confiabilidade](#confiabilidade-e-logs) · [Testes](#testes-e-qualidade) · [Docker](#docker) · [Estrutura](#estrutura)

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
| GET | `/api/v1/dashboard/forecast` | Qualquer usuário logado (projeção de 3 dias) |
| GET | `/api/v1/dashboard/alerts` | Qualquer usuário logado (`days`, `window`, `z_threshold`) |
| GET | `/api/v1/indicators/export` | Qualquer usuário logado (`start`, `end`, `base_temp`, `upper_temp`, `target_gdd`) |
| GET | `/api/v1/integrations/thingspeak/status` | Admin |
| POST | `/api/v1/integrations/thingspeak/sync` | Admin (`from` e `to` opcionais) |
| GET | `/health` | Público (liveness) |
| GET | `/ready` | Público (readiness) |

**Resolução do histórico** (`resolution`): `auto` usa leituras brutas até 48 h, por hora até 31 dias e por dia acima disso. `raw` é recusado se o período passar do limite de pontos. Os valores `hour` e `day` agregam a série. As métricas (média, mínimo, máximo) sempre usam as leituras brutas.

## Indicadores de exportação

`GET /api/v1/indicators/export` calcula, a partir de temperatura e umidade, o que o produtor precisa para planejar a exportação:

- **Graus-dia (GDD)**: por dia, `(Tmin + Tmax) / 2 - base`, com Tmin e Tmax limitados entre `base_temp` (padrão 10 °C) e `upper_temp` (padrão 35 °C). O acumulado soma só os dias completos.
- **Risco de doença** (por dia): horas favoráveis ao **míldio** (15 a 30 °C com umidade ≥ 90%) e ao **oídio** (20 a 27 °C com umidade ≥ 60%). O risco é `LOW` (< 4 h), `MODERATE` (4 a 7 h) ou `HIGH` (≥ 8 h).
- **Janela de colheita**: com o ritmo dos últimos 7 dias completos, projeta quando o acumulado chega a `target_gdd` (padrão 1500). Devolve a data mais cedo, a esperada e a mais tarde (P75, média e P25 do ritmo diário), além de quantos dos últimos dias tiveram risco alto de doença. O `status` é `REACHED`, `PROJECTED`, `BEYOND_HORIZON` (> 120 dias) ou `INSUFFICIENT_DATA` (< 3 dias recentes).

Parâmetros:

| Parâmetro | Padrão | Observação |
|---|---|---|
| `start`, `end` | últimos 30 dias | ISO 8601 com fuso. Use a **data da poda** em `start` para somar o ciclo certo. Informe os dois ou nenhum |
| `base_temp` | `10` | 0 a 20 °C |
| `upper_temp` | `35` | 20 a 50 °C; precisa ser maior que `base_temp` |
| `target_gdd` | `1500` | Valor de referência: **calibre por variedade** |

Exemplo:

```powershell
curl.exe -H "Authorization: Bearer <token>" "http://localhost:8000/api/v1/indicators/export?start=2026-09-01T00:00:00-03:00&end=2026-10-21T00:00:00-03:00&target_gdd=1400"
```

Campos principais da resposta: `gddAccumulated`, `completeDays`, `incompleteDays`, `currentDownyMildewRisk`, `currentPowderyMildewRisk`, `harvestWindow` e `days[]` (um item por dia, com GDD, acumulado e riscos). A lista `assumptions` descreve as regras usadas.

## Alertas e previsão

`GET /api/v1/dashboard/alerts?days=7&window=7&z_threshold=2` avalia os últimos `days` dias (1 a 60):

| Alerta | Regra | Severidade |
|---|---|---|
| `FROST` (geada) | temperatura mínima do dia ≤ 3 °C | `WARNING`; `CRITICAL` se ≤ 0 °C |
| `WATER_STRESS` | ≥ 4 h com DPV acima de 3 kPa | `WARNING`; `CRITICAL` com DPV acima de 4 kPa |
| `TEMPERATURE_ANOMALY` / `HUMIDITY_ANOMALY` | z-score da média diária contra os 14 dias completos anteriores (mínimo 7) ≥ `z_threshold` | `WARNING`; `CRITICAL` se ≥ 3 |

A resposta também traz `series[]` com a média diária, a **média móvel** (`window` dias, 2 a 30) e o z-score de temperatura e umidade.

`GET /api/v1/dashboard/forecast` devolve a **projeção dos próximos 3 dias** (temperatura e umidade) por regressão linear dos últimos 14 dias completos, com faixa `Low`/`High` de ± 1 desvio-padrão dos resíduos. Exige ao menos 7 dias completos; sem isso responde `available: false`.

### Limitações (leia antes de usar os números)

- Só existem **temperatura e umidade** (ThingSpeak, `field1` e `field2`). Não há chuva, molhamento foliar, umidade do solo nem radiação.
- **Estresse hídrico** é uma aproximação pelo déficit de pressão de vapor (DPV) do ar. Não mede a água disponível no solo. No semiárido, tardes quentes e secas passam de 3 kPa com frequência: ajuste os limites se o alerta disparar demais.
- **Risco de míldio e oídio** usa horas de temperatura e umidade favoráveis como substituto do molhamento foliar. É um indicativo, não um modelo validado em campo.
- **Geada** raramente ocorre no Vale do São Francisco; a regra existe para outras regiões e para falhas de sensor.
- A **previsão** é uma tendência estatística simples, não previsão meteorológica, e não foi comparada com outros métodos.
- Os limites (faixas de temperatura, horas, DPV, `target_gdd`) são valores de referência e devem ser calibrados com um agrônomo.
- **Dias completos** têm leitura em pelo menos 18 das 24 horas. Dias incompletos aparecem em `days[]`, mas não entram na soma de GDD nem nas anomalias; se houver algum, o acumulado é um valor mínimo. Leituras `REJECTED` são descartadas e contadas em `rejectedRecords`.
- A API Java devolve no máximo **5000 leituras por consulta** (as mais antigas do período). Os endpoints de indicadores, alertas e previsão dividem o período em consultas menores quando isso acontece, para não perder os dias mais recentes. `/climate/history` e os dashboards antigos ainda usam uma consulta só.
- Os dias são fechados no **fuso local** (`CLIMATE_UTC_OFFSET_HOURS`, padrão -3), diferente das séries de `/climate/history`, que agregam em UTC.

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
| `CLIMATE_UTC_OFFSET_HOURS` | `-3` | Fuso local do vinhedo (-12 a 14), usado para fechar os dias nos indicadores |

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
    daily.py           # leituras -> dias locais (média horária, Tmin/Tmax)
    indicators.py      # graus-dia, risco de doença, janela de colheita
    alerts.py          # médias móveis, anomalias, geada, estresse hídrico, previsão
  clients/java_api.py  # único ponto de acesso à API Java (com retry)
tests/                 # pytest
pyproject.toml         # configuração de ruff, mypy e pytest
.github/workflows/     # CI
```

## Contribuindo

1. Crie uma branch a partir da `main`.
2. Antes de abrir o Pull Request, rode `ruff`, `mypy` e `pytest`. O CI roda os mesmos três.
3. Não commite o `.env`.
