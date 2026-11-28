from __future__ import annotations

import asyncio
from datetime import date
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("agroclima.export_market")

COMEXSTAT_URL = "https://api-comexstat.mdic.gov.br/general"
COMEXSTAT_UPDATED_URL = "https://api-comexstat.mdic.gov.br/general/dates/updated"
NCM_UVA_FRESCA = "08061000"  # Uvas frescas

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "AgroClimaCloud/1.0",
}

# Cache em memória por 1h (evita rate-limit 429/500 da API pública do MDIC).
_CACHE: dict[tuple[int | None, int, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 3600.0


async def _ultimo_periodo_publicado(client: httpx.AsyncClient) -> tuple[int, int] | None:
    """Consulta no próprio MDIC qual é o último ano/mês já publicado."""
    try:
        headers = {"Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]}
        resp = await client.get(COMEXSTAT_UPDATED_URL, headers=headers)
        if resp.status_code == 200:
            dados = resp.json().get("data") or {}
            ano = int(dados.get("year") or 0)
            mes = int(dados.get("monthNumber") or 0)
            if ano >= 1997 and 1 <= mes <= 12:
                return ano, mes
    except Exception as exc:
        logger.debug("Não foi possível consultar /general/dates/updated: %s", exc)
    return None


def _periodo_padrao_seguro() -> tuple[int, int]:
    """Se /dates/updated falhar, usa 2 meses atrás (o MDIC publica com ~1 mês de defasagem)."""
    hoje = date.today()
    mes = hoje.month - 2
    ano = hoje.year
    if mes <= 0:
        mes += 12
        ano -= 1
    return ano, mes


async def _consultar_comexstat(
    client: httpx.AsyncClient,
    periodo_de: str,
    periodo_ate: str,
) -> list[dict[str, Any]] | None:
    payload = {
        "flow": "export",
        "monthDetail": False,
        "period": {
            "from": periodo_de,
            "to": periodo_ate,
        },
        "filters": [
            {
                "filter": "ncm",
                "values": [NCM_UVA_FRESCA],
            }
        ],
        "details": ["ncm"],
        "metrics": ["metricFOB", "metricKG"],
    }

    for tentativa in range(2):
        try:
            response = await client.post(
                COMEXSTAT_URL,
                json=payload,
                params={"language": "pt"},
                headers=_HEADERS,
            )
            if response.status_code in (429, 500, 502, 503, 504) and tentativa == 0:
                await asyncio.sleep(2.0)
                continue
            if response.status_code >= 400:
                logger.warning("ComexStat HTTP %s para %s..%s", response.status_code, periodo_de, periodo_ate)
                return None
            body = response.json()
            dados = body.get("data")
            registros = (dados.get("list") if isinstance(dados, dict) else dados) or []
            return registros if isinstance(registros, list) else []
        except httpx.HTTPError as exc:
            logger.warning("Falha HTTP ao consultar ComexStat (%s..%s): %s", periodo_de, periodo_ate, exc)
            if tentativa == 0:
                await asyncio.sleep(1.5)
    return None


async def obter_valor_exportacao_uva(
    ano: int | None = None,
    mes_inicio: int = 1,
    mes_fim: int = 12,
) -> dict[str, Any]:
    """
    Busca o valor de exportação de uvas frescas (NCM 08061000)
    na API oficial do ComexStat (MDIC), respeitando o último mês publicado.
    """
    chave = (ano, mes_inicio, mes_fim)
    agora = time.monotonic()
    em_cache = _CACHE.get(chave)
    if em_cache is not None and (agora - em_cache[0]) < _CACHE_TTL_SECONDS:
        return em_cache[1]

    # verify=False evita erro de cadeia ICP-Brasil em containers slim; nunca levanta 502 na interface.
    async with httpx.AsyncClient(timeout=25.0, verify=False) as client:
        publicado = await _ultimo_periodo_publicado(client)
        ult_ano, ult_mes = publicado if publicado is not None else _periodo_padrao_seguro()

        ano_alvo = ult_ano if ano is None else ano
        fim_alvo = min(mes_fim, ult_mes) if ano_alvo >= ult_ano else mes_fim
        if ano_alvo > ult_ano:
            ano_alvo, fim_alvo = ult_ano, ult_mes

        periodo_de = f"{ano_alvo}-{mes_inicio:02d}"
        periodo_ate = f"{ano_alvo}-{max(mes_inicio, fim_alvo):02d}"

        registros = await _consultar_comexstat(client, periodo_de, periodo_ate)

        # Se o ano corrente falhar na API do MDIC, tenta o último ano completo fechado como fallback real.
        if registros is None and ano is None and ano_alvo == date.today().year:
            ano_anterior = ano_alvo - 1
            periodo_de = f"{ano_anterior}-01"
            periodo_ate = f"{ano_anterior}-12"
            registros = await _consultar_comexstat(client, periodo_de, periodo_ate)

    if not registros:
        return {
            "disponivel": False,
            "mensagem": (
                "A API pública do ComexStat (MDIC) está temporariamente indisponível no momento."
                if registros is None
                else "Nenhum dado de exportação encontrado para o período."
            ),
            "ncm": NCM_UVA_FRESCA,
            "produto": "Uvas frescas",
            "periodo": f"{periodo_de} a {periodo_ate}",
        }

    total_fob_usd = 0.0
    total_kg = 0.0

    for item in registros:
        total_fob_usd += float(item.get("metricFOB") or item.get("FOB") or 0)
        total_kg += float(item.get("metricKG") or item.get("KG") or 0)

    preco_medio_usd_kg = (total_fob_usd / total_kg) if total_kg > 0 else None

    resultado: dict[str, Any] = {
        "disponivel": True,
        "ncm": NCM_UVA_FRESCA,
        "produto": "Uvas frescas",
        "periodo": f"{periodo_de} a {periodo_ate}",
        "valor_exportado_usd": round(total_fob_usd, 2),
        "quantidade_kg": round(total_kg, 2),
        "quantidade_toneladas": round(total_kg / 1000, 2),
        "preco_medio_usd_kg": round(preco_medio_usd_kg, 4) if preco_medio_usd_kg else None,
        "fonte": "ComexStat / MDIC",
        "atualizado_em": date.today().isoformat(),
    }
    _CACHE[chave] = (agora, resultado)
    return resultado
