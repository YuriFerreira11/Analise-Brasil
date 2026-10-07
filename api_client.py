from datetime import date
import requests
import pandas as pd

class ApiClient:
    BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
    def buscar_serie(self,
        codigo_serie: int,
        data_inicial: date | None = None,
        data_final: date | None = None) -> pd.DataFrame:
        url = self.BASE_URL.format(codigo_serie=codigo_serie)
        params = {"formato": "json"}
        if data_inicial:
            params["dataInicial"] = data_inicial.strftime("%d/%m/%Y")
        if data_final:
            params["dataFinal"] = data_final.strftime("%d/%m/%Y")

        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        dados = response.json()
        if not isinstance(dados, list):
            raise ValueError(f"Resposta inesperada para a série {codigo_serie}: {str(dados)[:200]}")
        return pd.DataFrame(dados)