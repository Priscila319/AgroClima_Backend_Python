from __future__ import annotations

from datetime import date
from typing import Any

import httpx

COMEXSTAT_URL = "https://api-comexstat.mdic.gov.br/general"
NCM_UVA_FRESCA = "08061000"  # Uvas frescas


async def obter_valor_exportacao_uva(
    ano: int | None = None,
    mes_inicio: int = 1,
    mes_fim: int = 12,
) -> dict[str, Any]:
    """
    Busca o valor de exportação de uvas frescas (NCM 08061000)
    na API oficial do ComexStat (MDIC).
    """
    if ano is None:
        ano = date.today().year

    periodo_de = f"{ano}-{mes_inicio:02d}"
    periodo_ate = f"{ano}-{mes_fim:02d}"

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

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            COMEXSTAT_URL,
            json=payload,
            params={"language": "pt"},
        )
        response.raise_for_status()
        data = response.json()

    dados = data.get("data")
    registros = (dados.get("list") if isinstance(dados, dict) else dados) or []

    if not registros:
        return {
            "disponivel": False,
            "mensagem": "Nenhum dado de exportação encontrado para o período.",
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

    return {
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
