import pandas as pd
from datetime import date, timedelta
from src.adapters.bcb_api import ApiClient
from src.database.db import get_engine
from sqlalchemy import text
import pandera.pandas as pa
import logging
from src.adapters.focus_api import FocusApiClient

SERIES = {
    "taxa_selic": {"codigo": 432,   "agregacao": "last"},
    "ipca_pct":   {"codigo": 433,   "agregacao": "last"},
    "cambio_usd": {"codigo": 1,     "agregacao": "mean"},
    "igpm":       {"codigo": 189,   "agregacao": "last"},
    "ibcbr":      {"codigo": 24364, "agregacao": "last", "inicio": date(2003, 1, 1)},
    "desemprego":  {"codigo": 24369, "agregacao": "last", "inicio": date(2012, 1, 1)},
    "ipca_focus":  {"fonte": "focus", "agregacao": "last"},

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
    "ibcbr": (50, 300),
    "desemprego": (0, 30),
    "ipca_focus": (0, 30),
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
def extract(ultimas_datas=None, full_refresh=False):
    cliente_bcb = ApiClient(timeout=60)
    cliente_focus = FocusApiClient(timeout=60)
    hoje = date.today()
    frames = {nome: [] for nome in SERIES}

    series_bcb = {n: c for n, c in SERIES.items() if c.get("fonte") != "focus"}
    series_focus = {n: c for n, c in SERIES.items() if c.get("fonte") == "focus"}

    for ano in range(DATA_INICIAL.year, hoje.year + 1):
        inicio_ano = date(ano, 1, 1)
        fim = min(date(ano, 12, 31), hoje)
        n_req = 0
        for nome, cfg in series_bcb.items():
            inicio = max(inicio_ano, cfg.get("inicio", DATA_INICIAL))
            if inicio > fim:
                continue
            if not full_refresh and ultimas_datas and ultimas_datas.get(nome):
                ultima = ultimas_datas[nome]
                if ultima >= fim:
                    continue
                inicio = max(inicio, ultima + timedelta(days=1))
            frames[nome].append(
                cliente_bcb.buscar_serie(cfg["codigo"], data_inicial=inicio, data_final=fim)
            )
            n_req += 1
        logger.info("ano %d: %d requisições", ano, n_req)

    for nome, cfg in series_focus.items():
        frames[nome].append(
            cliente_focus.buscar_serie(None, data_inicial=DATA_INICIAL, data_final=hoje)
        )
        logger.info("focus: 1 requisição (cobre 2000→hoje)")

    return {
        nome: pd.concat(lista, ignore_index=True) if lista
              else pd.DataFrame(columns=["data", "valor"])
        for nome, lista in frames.items()
    }

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

    engine = get_engine()
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

