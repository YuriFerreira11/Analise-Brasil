import pandas as pd
from datetime import date
from api_client import ApiClient
from sqlalchemy import create_engine, text
import os

SERIES = {"selic": 432, "ipca": 433}
DATA_INICIAL = date(2020, 1, 1)
COLUNAS = {"selic": "taxa_selic", "ipca": "ipca_pct"}

def extract() -> dict[str, pd.DataFrame]:
    cliente = ApiClient()

    """Extrai as séries do Banco Central e devolve um DataFrame por série."""
    return {
        nome: cliente.buscar_serie(codigo, data_inicial=DATA_INICIAL, data_final=date.today())
        for nome, codigo in SERIES.items()
    }

def transform(dados_brutos: dict[str, pd.DataFrame]) -> pd.DataFrame:
    dados_limpos = {}
    for nome, df_bruto in dados_brutos.items():
        nome_valor = COLUNAS[nome]
        df = df_bruto.copy()

        df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
        df["valor"] = pd.to_numeric(df["valor"], errors="coerce")

        if df['valor'].isna().any():
            raise ValueError(f"{nome_valor}: {int(df['valor'].isna().sum())} valores inválidos")
        dados_limpos[nome] = (
            df.drop_duplicates("data").
            sort_values("data").
            rename(columns={"valor": nome_valor}).
            reset_index(drop=True)
        )
        selic_m = dados_limpos["selic"].set_index("data")["taxa_selic"].resample("MS").last()
        ipca_m = dados_limpos["ipca"].assign(
            data=dados_limpos["ipca"]["data"].dt.to_period("M").dt.to_timestamp()
        ).set_index("data")["ipca_pct"]

        # Faz o join das duas séries pelo índice de data e retorna o DataFrame consolidado
        df_consolidado = pd.concat([selic_m, ipca_m], axis=1, join="inner").reset_index()

        return df_consolidado
def load(df: pd.DataFrame, tabela: str, schema: str = "staging") -> int:
    if df.empty:
        return 0

    engine = create_engine(os.environ["DATABASE_URL"])

    with engine.begin() as conn:  # transação única: erro => rollback
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {schema}.{tabela} (
                data        DATE PRIMARY KEY,
                taxa_selic  NUMERIC(10, 4),
                ipca_pct    NUMERIC(10, 4)
            )
        """))
        conn.execute(
            text(f"""
                INSERT INTO {schema}.{tabela} (data, taxa_selic, ipca_pct)
                VALUES (:data, :taxa_selic, :ipca_pct)
                ON CONFLICT (data) DO UPDATE SET
                    taxa_selic = EXCLUDED.taxa_selic,
                    ipca_pct   = EXCLUDED.ipca_pct
            """),
            df[["data", "taxa_selic", "ipca_pct"]].to_dict("records"),
        )

    return len(df)

