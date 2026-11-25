from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

# Caminho seguro até: backend-python/data/preco_uva_vinho.csv
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
PRECO_UVA_CSV = DATA_DIR / "preco_uva_vinho.csv"


def listar_precos_uva(
    ano_safra: str | None = None,
    variedade_grupo: str | None = None,
    regiao_praca: str | None = None,
) -> list[dict[str, Any]]:
    if not PRECO_UVA_CSV.exists():
        raise FileNotFoundError(f"Arquivo CSV não encontrado em: {PRECO_UVA_CSV}")

    resultados: list[dict[str, Any]] = []

    # utf-8-sig lê normal mesmo se o arquivo tiver BOM do Windows/Excel
    with PRECO_UVA_CSV.open(mode="r", encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)

        for linha in leitor:
            safra = (linha.get("ano_safra") or "").strip()
            variedade = (linha.get("variedade_grupo") or "").strip()
            regiao = (linha.get("regiao_praca") or "").strip()
            preco_str = (linha.get("preco_brl_kg") or "").strip()

            # Filtros opcionais (se o usuário informar)
            if ano_safra and safra != ano_safra.strip():
                continue
            if variedade_grupo and variedade_grupo.strip().lower() not in variedade.lower():
                continue
            if regiao_praca and regiao_praca.strip().lower() not in regiao.lower():
                continue

            resultados.append(
                {
                    "ano_safra": int(safra) if safra.isdigit() else safra,
                    "tipo_dado": (linha.get("tipo_dado") or "").strip(),
                    "variedade_grupo": variedade,
                    "grau_glucometrico": (linha.get("grau_glucometrico") or "").strip(),
                    "regiao_praca": regiao,
                    "preco_brl_kg": float(preco_str) if preco_str else None,
                    "fonte_normativa": (linha.get("fonte_normativa") or "").strip(),
                }
            )

    return resultados

VALOR_PRODUCAO_CSV = DATA_DIR / "uva-valor-da-produo-2025.csv"


def _parse_valor_milhar(valor_raw: str) -> float | None:
    limpo = valor_raw.strip()
    if not limpo or limpo in {"-", "...", "X"}:
        return None
    # Remove separador de milhar (ex.: "4.205.265" -> "4205265") e troca vírgula decimal por ponto se houver
    normalizado = limpo.replace(".", "").replace(",", ".")
    try:
        return float(normalizado)
    except ValueError:
        return None


def listar_valor_producao_uva(
    localidade: str | None = None,
    unidade: str | None = None,
) -> list[dict[str, Any]]:
    if not VALOR_PRODUCAO_CSV.exists():
        raise FileNotFoundError(f"Arquivo CSV não encontrado em: {VALOR_PRODUCAO_CSV}")

    resultados: list[dict[str, Any]] = []

    with VALOR_PRODUCAO_CSV.open(mode="r", encoding="utf-8-sig", newline="") as arquivo:
        # Pula a linha de título ("Uva - Valor da produção (2025),,")
        next(arquivo, None)
        leitor = csv.DictReader(arquivo)

        for linha in leitor:
            loc = (linha.get("localidade") or "").strip()
            valor_raw = (linha.get("valor") or "").strip()
            unid = (linha.get("unidade") or "").strip()

            if not loc and not valor_raw and not unid:
                continue

            # Ignora a linha de rodapé do IBGE ("Fonte: PAM")
            if loc.lower().startswith("fonte:"):
                continue

            if localidade and localidade.strip().lower() not in loc.lower():
                continue
            if unidade and unidade.strip().lower() not in unid.lower():
                continue

            valor_num = _parse_valor_milhar(valor_raw)

            resultados.append(
                {
                    "ano": 2025,
                    "localidade": loc,
                    "valor": valor_num,
                    "unidade": unid,
                }
            )

    return resultados