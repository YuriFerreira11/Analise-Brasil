import logging
import time
from datetime import date

import pandas as pd
import requests

logger = logging.getLogger("bcb_api")


class ApiClient:
    BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def buscar_serie(self,
                     codigo_serie: int,
                     data_inicial: date | None = None,
                     data_final: date | None = None,
                     tentativas: int = 3) -> pd.DataFrame:
        """Busca uma série no SGS do Banco Central com retry e backoff.

        Retenta automaticamente em falhas recuperáveis:
        - timeout e erro de conexão
        - erro 5xx do servidor (502/503/504)
        - resposta inválida (vazia, HTML, ou JSON que não é lista)

        Erros 4xx (URL errada, série inexistente) falham imediatamente —
        repetir não ajuda. Após a última tentativa, falha alto para que o
        caller registre a execução como 'erro'.
        """
        url = self.BASE_URL.format(codigo=codigo_serie)
        params = {"formato": "json"}
        if data_inicial:
            params["dataInicial"] = data_inicial.strftime("%d/%m/%Y")
        if data_final:
            params["dataFinal"] = data_final.strftime("%d/%m/%Y")

        for tentativa in range(1, tentativas + 1):
            try:
                response = requests.get(url, params=params, timeout=self.timeout)

                if response.status_code >= 500:
                    raise requests.HTTPError(f"{response.status_code} do servidor",
                                             response=response)
                response.raise_for_status()

                dados = response.json()
                if not isinstance(dados, list):
                    raise ValueError(
                        f"Resposta inesperada para a série {codigo_serie}: {str(dados)[:200]}"
                    )
                return pd.DataFrame(dados)

            except (requests.ReadTimeout, requests.ConnectionError,
                    requests.HTTPError,
                    requests.exceptions.JSONDecodeError, ValueError) as e:
                if tentativa == tentativas:
                    raise
                espera = 2 * tentativa
                logger.warning("tentativa %d falhou (%s), aguardando %ds",
                               tentativa, type(e).__name__, espera)
                time.sleep(espera)