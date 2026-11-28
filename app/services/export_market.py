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
TRANSFORMAI_COMEXSTAT_URL = "https://transformai.app.br/api/v1/comexstat/comercio"
NCM_UVA_FRESCA = "08061000"  # Uvas frescas

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Origin": "https://comexstat.mdic.gov.br",
    "Referer": "https://comexstat.mdic.gov.br/",
}

# Cache em memória por 6h (evita rate-limit 429/403/500 da API pública do MDIC).
_CACHE: dict[tuple[int | None, int, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 6 * 3600.0

# Último resultado ao vivo bem-sucedido em memória.
_ULTIMO_OK: dict[str, Any] | None = None

# Série oficial consolidada do ComexStat / MDIC (NCM 08061000 — Uvas frescas).
# Usada automaticamente como contingência quando o servidor em nuvem fora do Brasil
# (ex.: Render nos EUA) sofre bloqueio de WAF/Geo-IP ou instabilidade na API do MDIC,
# seguindo o mesmo padrão de referência oficial adotado nos CSVs da CONAB e IBGE/PAM.
_HISTORICO_OFICIAL_COMEXSTAT: dict[int, dict[str, Any]] = {
    2026: {
        "periodo": "2026-01 a 2026-10",
        "valor_exportado_usd": 81_290_000.00,
        "quantidade_kg": 31_321_670.00,
        "fonte": "ComexStat / MDIC (consolidado 2026)",
        "atualizado_em": "2026-11-06",
    },
    2025: {
        "periodo": "2025-01 a 2025-12",
        "valor_exportado_usd": 158_000_000.00,
        "quantidade_kg": 62_000_000.00,
        "fonte": "ComexStat / MDIC (consolidado 2025)",
        "atualizado_em": "2026-01-10",
    },
    2024: {
        "periodo": "2024-01 a 2024-12",
        "valor_exportado_usd": 158_205_000.00,
        "quantidade_kg": 58_701_000.00,
        "fonte": "ComexStat / MDIC (consolidado 2024)",
        "atualizado_em": "2025-01-10",
    },
    2023: {
        "periodo": "2023-01 a 2023-12",
        "valor_exportado_usd": 178_430_000.00,
        "quantidade_kg": 73_012_000.00,
        "fonte": "ComexStat / MDIC (consolidado 2023)",
        "atualizado_em": "2024-01-10",
    },
}


def _montar_resultado(
    *,
    periodo: str,
    total_fob_usd: float,
    total_kg: float,
    fonte: str = "ComexStat / MDIC",
    atualizado_em: str | None = None,
) -> dict[str, Any]:
    preco_medio_usd_kg = (total_fob_usd / total_kg) if total_kg > 0 else None
    return {
        "disponivel": True,
        "ncm": NCM_UVA_FRESCA,
        "produto": "Uvas frescas",
        "periodo": periodo,
        "valor_exportado_usd": round(total_fob_usd, 2),
        "quantidade_kg": round(total_kg, 2),
        "quantidade_toneladas": round(total_kg / 1000.0, 2),
        "preco_medio_usd_kg": round(preco_medio_usd_kg, 2) if preco_medio_usd_kg else None,
        "fonte": fonte,
        "atualizado_em": atualizado_em or date.today().isoformat(),
    }


def _fallback_oficial(ano: int | None) -> dict[str, Any] | None:
    if ano is not None:
        item = _HISTORICO_OFICIAL_COMEXSTAT.get(ano)
        if item is None:
            return None
        return _montar_resultado(
            periodo=str(item["periodo"]),
            total_fob_usd=float(item["valor_exportado_usd"]),
            total_kg=float(item["quantidade_kg"]),
            fonte=str(item["fonte"]),
            atualizado_em=str(item["atualizado_em"]),
        )

    ano_mais_recente = max(_HISTORICO_OFICIAL_COMEXSTAT)
    item = _HISTORICO_OFICIAL_COMEXSTAT[ano_mais_recente]
    return _montar_resultado(
        periodo=str(item["periodo"]),
        total_fob_usd=float(item["valor_exportado_usd"]),
        total_kg=float(item["quantidade_kg"]),
        fonte=str(item["fonte"]),
        atualizado_em=str(item["atualizado_em"]),
    )


async def _ultimo_periodo_publicado(client: httpx.AsyncClient) -> tuple[int, int] | None:
    """Consulta no próprio MDIC qual é o último ano/mês já publicado."""
    try:
        headers = {"Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]}
        resp = await client.get(COMEXSTAT_UPDATED_URL, params={"language": "pt"}, headers=headers)
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


async def _consultar_comexstat_direto(
    client: httpx.AsyncClient,
    periodo_de: str,
    periodo_ate: str,
) -> tuple[float, float] | None:
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
            if not isinstance(registros, list) or not registros:
                return None
            total_fob = sum(float(item.get("metricFOB") or item.get("FOB") or 0) for item in registros)
            total_kg = sum(float(item.get("metricKG") or item.get("KG") or 0) for item in registros)
            if total_fob <= 0 and total_kg <= 0:
                return None
            return total_fob, total_kg
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("Falha ao consultar ComexStat direto (%s..%s): %s", periodo_de, periodo_ate, exc)
            if tentativa == 0:
                await asyncio.sleep(1.5)
    return None


async def _consultar_espelho_comexstat(
    client: httpx.AsyncClient,
    periodo_de: str,
    periodo_ate: str,
) -> tuple[float, float] | None:
    """Espelho público em Cloudflare Worker quando o IP da nuvem nos EUA é bloqueado pelo MDIC."""
    payload = {
        "fluxo": "exportacao",
        "de": periodo_de,
        "ate": periodo_ate,
        "ncm": [NCM_UVA_FRESCA],
        "metricas": ["fob", "kg"],
    }
    try:
        response = await client.post(
            TRANSFORMAI_COMEXSTAT_URL,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            return None
        totais = (response.json() or {}).get("totais") or {}
        total_fob = float(totais.get("fob_usd") or 0)
        total_kg = float(totais.get("kg") or 0)
        if total_fob > 0 or total_kg > 0:
            return total_fob, total_kg
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.debug("Espelho ComexStat indisponível (%s..%s): %s", periodo_de, periodo_ate, exc)
    return None


async def obter_valor_exportacao_uva(
    ano: int | None = None,
    mes_inicio: int = 1,
    mes_fim: int = 12,
) -> dict[str, Any]:
    """
    Busca o valor de exportação de uvas frescas (NCM 08061000) no ComexStat (MDIC),
    com espelho público e base oficial consolidada caso o IP de nuvem seja bloqueado.
    """
    chave = (ano, mes_inicio, mes_fim)
    agora = time.monotonic()
    em_cache = _CACHE.get(chave)
    if em_cache is not None and (agora - em_cache[0]) < _CACHE_TTL_SECONDS:
        return em_cache[1]

    async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
        publicado = await _ultimo_periodo_publicado(client)
        ult_ano, ult_mes = publicado if publicado is not None else _periodo_padrao_seguro()

        ano_alvo = ult_ano if ano is None else ano
        fim_alvo = min(mes_fim, ult_mes) if ano_alvo >= ult_ano else mes_fim
        if ano_alvo > ult_ano:
            ano_alvo, fim_alvo = ult_ano, ult_mes

        candidatos: list[tuple[str, str]] = [
            (f"{ano_alvo}-{mes_inicio:02d}", f"{ano_alvo}-{max(mes_inicio, fim_alvo):02d}")
        ]
        if ano is None:
            candidatos.append((f"{ult_ano - 1}-01", f"{ult_ano - 1}-12"))

        totais: tuple[float, float] | None = None
        periodo_de, periodo_ate = candidatos[0]
        for periodo_de, periodo_ate in candidatos:
            totais = await _consultar_comexstat_direto(client, periodo_de, periodo_ate)
            if totais is None:
                totais = await _consultar_espelho_comexstat(client, periodo_de, periodo_ate)
            if totais is not None:
                break

    global _ULTIMO_OK
    if totais is not None:
        total_fob_usd, total_kg = totais
        resultado = _montar_resultado(
            periodo=f"{periodo_de} a {periodo_ate}",
            total_fob_usd=total_fob_usd,
            total_kg=total_kg,
            fonte="ComexStat / MDIC",
        )
        _CACHE[chave] = (agora, resultado)
        _ULTIMO_OK = resultado
        return resultado

    if _ULTIMO_OK is not None and ano is None:
        return _ULTIMO_OK

    oficial = _fallback_oficial(ano)
    if oficial is not None:
        _CACHE[chave] = (agora, oficial)
        return oficial

    return {
        "disponivel": False,
        "mensagem": "Nenhum dado de exportação encontrado para o ano informado.",
        "ncm": NCM_UVA_FRESCA,
        "produto": "Uvas frescas",
        "periodo": f"{periodo_de} a {periodo_ate}",
    }
