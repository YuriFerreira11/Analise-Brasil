"""Cliente da API de Expectativas de Mercado (Boletim Focus) do Banco Central.

API OData: https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/

Entidade: ExpectativasMercadoAnuais (expectativas por ANO-calendário).
Regra da feature "ipca_focus": para cada data de expectativa, usamos a
Mediana de IPCA cujo DataReferencia = ano de (Data + 12 meses) —
aproximação da expectativa de inflação acumulada em 12 meses.
(Confirmado em 2026-10-13: $filter=Indicador eq 'IPCA' funciona no navegador;
o 400 esporádico via requests é tratado com fallback sem filtro.)

Contrato de saída idêntico ao ApiClient do SGS: DataFrame 'data'/'valor',
então o transform não percebe a troca de fonte.
"""
import logging
import time
from datetime import date

import pandas as pd
import requests

logger = logging.getLogger("focus_api")

BASE_URL = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/"
            "versao/v1/odata/ExpectativasMercadoAnuais")
VAZIO = pd.DataFrame(columns=["data", "valor"])


class FocusApiClient:
    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def buscar_serie(self,
                     codigo_serie: int | None = None,   # ignorado: mantém contrato do SGS
                     data_inicial: date | None = None,
                     data_final: date | None = None,
                     tentativas: int = 3) -> pd.DataFrame:
        for tentativa in range(1, tentativas + 1):
            try:
                df = self._fetch()
                break
            except (requests.ReadTimeout, requests.ConnectionError) as e:
                if tentativa == tentativas:
                    raise
                espera = 2 * tentativa
                logger.warning("tentativa %d falhou (%s), aguardando %ds",
                               tentativa, type(e).__name__, espera)
                time.sleep(espera)
            except requests.HTTPError as e:
                # 400 esporádico do proxy OData: uma tentativa sem filtro resolve
                if tentativa == 1 and e.response is not None and e.response.status_code == 400:
                    logger.warning("filtro OData rejeitado (400); fallback sem filtro + filtro client-side")
                    df = self._fetch_sem_filtro()
                    break
                raise

        df["Data"] = pd.to_datetime(df["Data"])

        # REGRA: expectativa para o ano-calendário 12 meses à frente da data
        alvo = (df["Data"] + pd.DateOffset(months=12)).dt.year.astype(str)
        df = df[df["DataReferencia"] == alvo]

        if data_inicial:
            df = df[df["Data"] >= pd.Timestamp(data_inicial)]
        if data_final:
            df = df[df["Data"] <= pd.Timestamp(data_final)]
        if df.empty:
            return VAZIO.copy()

        return pd.DataFrame({
            "data": df["Data"].dt.strftime("%d/%m/%Y"),
            "valor": pd.to_numeric(df["Mediana"], errors="coerce"),
        })

    def _fetch(self, usar_filtro: bool = True) -> pd.DataFrame:
        filtro = "%24filter=Indicador%20eq%20%27IPCA%27&" if usar_filtro else ""
        partes, skip = [], 0
        while True:
            url = (f"{BASE_URL}?{filtro}"
                   "%24select=Data%2CDataReferencia%2CMediana&"
                   "%24orderby=Data&%24format=json&"
                   f"%24top=10000&%24skip={skip}")
            resp = requests.get(url, timeout=self.timeout)
    
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} do servidor", response=resp)
            resp.raise_for_status()
            pagina = resp.json().get("value", [])
            if not pagina:
                break
            partes.append(pd.DataFrame(pagina))
            skip += 10000
            if len(pagina) < 10000:
                break
        df = pd.concat(partes, ignore_index=True)
        if not usar_filtro:
            df = df[df["Indicador"] == "IPCA"] if "Indicador" in df.columns else df.iloc[0:0]
        return df

    def _fetch_sem_filtro(self) -> pd.DataFrame:
        return self._fetch(usar_filtro=False)
