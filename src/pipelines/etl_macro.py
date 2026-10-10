import pandas as pd
from datetime import date
from src.adapters.bcb_api import ApiClient
from sqlalchemy import create_engine, text
import os
import pandera.pandas as pa
import logging

SERIES = {
    "taxa_selic": {"codigo": 432,   "agregacao": "last"},
    "ipca_pct":   {"codigo": 433,   "agregacao": "last"},
    "cambio_usd": {"codigo": 1,     "agregacao": "mean"},
    "igpm":       {"codigo": 189,   "agregacao": "last"},
    "ibcbr":      {"codigo": 4380,  "agregacao": "last"},
}
DATA_INICIAL = date(2000, 1, 1)
# Contrato do DataFrame consolidado: quem consumir esses dados pode confiar
# que data é única e os indicadores estão em range plausível.
# Taxas: range plausível fixo (economia limita esses valores)
RANGES = {
    "taxa_selic": (0, 60),
    "ipca_pct": (-5, 30),
    "cambio_usd": (0, 20),
    "igpm": (-10, 40),
}

# Níveis (valores absolutos que crescem com o tempo): só limites físicos.
# Variação mensal absurda (ex: salto >50% em um mês) é o sinal real de erro.
NIVEIS = {"ibcbr": 0}

def _variacao_mensal_ok(s: pd.Series) -> bool:
    return bool((s.dropna().pct_change().abs().dropna() < 0.50).all())

SCHEMA_INDICADORES = pa.DataFrameSchema({
    "data": pa.Column(pa.DateTime, unique=True, nullable=False),
    **{col: pa.Column(float, pa.Check.in_range(lo, hi), nullable=True)
       for col, (lo, hi) in RANGES.items()},                      # <- faltava aplicar
    **{col: pa.Column(float,
                      [pa.Check.ge(piso),
                       pa.Check(_variacao_mensal_ok,
                                error="variação mensal > 50% — provável erro de dado")],
                      nullable=True)
       for col, piso in NIVEIS.items()},
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
        for nome, cfg in SERIES.items():
            frames[nome].append(
                cliente.buscar_serie(cfg["codigo"], data_inicial=inicio, data_final=fim)
            )
        logger.info("ano %d extraído", ano)

    return {nome: pd.concat(df_list, ignore_index=True) for nome, df_list in frames.items()}

def transform(dados_brutos: dict[str, pd.DataFrame]) -> pd.DataFrame:
    mensais = []
    for nome, df_bruto in dados_brutos.items():
        df = df_bruto.copy()
        if df.empty:
            raise ValueError(f"{nome}: série vazia")

        try:
            df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
        except ValueError as e:
            raise ValueError(f"{nome}: data inválida ({e})") from e
        df["valor"] = pd.to_numeric(df["valor"], errors="coerce")

        if df["valor"].isna().any():
            raise ValueError(f"{nome}: {int(df['valor'].isna().sum())} valores inválidos")
        df["valor"] = df["valor"].astype(float)
        df = df.drop_duplicates("data")
        serie = df.set_index("data")["valor"]
        mensais.append(serie.resample("MS").agg(SERIES[nome]["agregacao"]).rename(nome))

    df_consolidado = pd.concat(mensais, axis=1, join="outer").reset_index()
    df_consolidado["data"] = df_consolidado["data"].dt.to_period("M").dt.to_timestamp()
    return SCHEMA_INDICADORES.validate(df_consolidado)

def load(df: pd.DataFrame, tabela: str, schema: str = "staging") -> int:
    """Carga idempotente (upsert por data) em transação única."""
    if df.empty:
        return 0

    cols = list(df.columns)
    colunas_tipo = ", ".join(f"{c} NUMERIC(14, 4)" for c in cols if c != "data")
    update_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "data")

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:  # transação única: erro => rollback
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {schema}.{tabela} (
                data DATE PRIMARY KEY,
                {colunas_tipo}
            )
        """))
        conn.execute(
            text(f"""
                INSERT INTO {schema}.{tabela} ({", ".join(cols)})
                VALUES ({", ".join(f":{c}" for c in cols)})
                ON CONFLICT (data) DO UPDATE SET {update_sql}
            """),
            df.to_dict("records"),
        )

    return len(df)


def registrar_execucao(engine, tabela: str, linhas_lidas: int,
                       linhas_carregadas: int, status: str,
                       duracao_seg: float, detalhe: str = "") -> None:
    """Auditoria: toda execução deixa rastro, com sucesso ou erro."""
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

