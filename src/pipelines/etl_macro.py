import pandas as pd
from datetime import date
from src.adapters.bcb_api import ApiClient
from sqlalchemy import create_engine, text
import os
import pandera.pandas as pa
import logging

SERIES = {"selic": 432, "ipca": 433}
DATA_INICIAL = date(2020, 1, 1)
COLUNAS = {"selic": "taxa_selic", "ipca": "ipca_pct"}
# Contrato do DataFrame consolidado: quem consumir esses dados pode confiar
# que data é única e os indicadores estão em range plausível.
SCHEMA_INDICADORES = pa.DataFrameSchema({
    "data": pa.Column(pa.DateTime, unique=True, nullable=False),
    "taxa_selic": pa.Column(float, pa.Check.in_range(0, 60), nullable=False),
    "ipca_pct": pa.Column(float, pa.Check.in_range(-5, 30), nullable=False),
})
logger = logging.getLogger("etl_macro")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
def extract() -> dict[str, pd.DataFrame]:
    """Extrai as séries do Banco Central e devolve um DataFrame por série."""
    cliente = ApiClient(timeout=60)
    hoje = date.today()
    frames = {nome: [] for nome in SERIES}

    for ano in range(DATA_INICIAL.year, hoje.year + 1):
        inicio = date(ano, 1, 1)
        fim = min(date(ano, 12, 31), hoje)
        for nome, codigo in SERIES.items():
            frames[nome].append(
                cliente.buscar_serie(codigo, data_inicial=inicio, data_final=fim)
            )
    logger.info("ano %d extraído", ano)

    return {nome: pd.concat(df_list, ignore_index=True) for nome, df_list in frames.items()}

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
            df.drop_duplicates("data")
            .rename(columns={"valor": nome_valor})
            .reset_index(drop=True)
        )
    selic_m = dados_limpos["selic"].set_index("data")["taxa_selic"].resample("MS").last()
    ipca_m = dados_limpos["ipca"].assign(
        data=dados_limpos["ipca"]["data"].dt.to_period("M").dt.to_timestamp()
    ).set_index("data")["ipca_pct"]

    # Faz o join das duas séries pelo índice de data e retorna o DataFrame consolidado
    df_consolidado = pd.concat([selic_m, ipca_m], axis=1, join="inner").reset_index()
    return SCHEMA_INDICADORES.validate(df_consolidado)


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


def registrar_execucao(engine, tabela: str, linhas_lidas: int,
                       linhas_carregadas: int, status: str,
                       duracao_seg: float, detalhe: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS staging.execucoes (
                id              BIGSERIAL PRIMARY KEY,
                executado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
                tabela          TEXT NOT NULL,
                linhas_lidas    INTEGER,
                linhas_carregadas INTEGER,
                status          TEXT NOT NULL,
                duracao_seg     NUMERIC(10, 2),
                detalhe         TEXT
            )
        """))
        conn.execute(
            text("""
                INSERT INTO staging.execucoes
                    (tabela, linhas_lidas, linhas_carregadas, status, duracao_seg, detalhe)
                VALUES (:tabela, :linhas_lidas, :linhas_carregadas, :status, :duracao_seg, :detalhe)
            """),
            dict(tabela=tabela, linhas_lidas=linhas_lidas,
                 linhas_carregadas=linhas_carregadas, status=status,
                 duracao_seg=duracao_seg, detalhe=detalhe),
        )

