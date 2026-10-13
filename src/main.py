import time
import logging
from datetime import date
import sys
import pandas as pd
from src.database.db import get_engine
from dotenv import load_dotenv
load_dotenv()

from src.pipelines.etl_macro import extract, transform, load, registrar_execucao
logger = logging.getLogger("main")
def ultimas_datas_por_serie(engine) -> dict[str, date] | None:
    try:
        df_db = pd.read_sql("SELECT * FROM staging.indicadores_macro", engine)
    except Exception:         
        return None             
    df_db["data"] = pd.to_datetime(df_db["data"])
    return {
        col: df_db.loc[df_db[col].notna(), "data"].max().date()
        for col in df_db.columns if col != "data"
    }
def main():
    inicio = time.monotonic()
    engine = get_engine()
    full_refresh = "--full" in sys.argv

    ultimas = None if full_refresh else ultimas_datas_por_serie(engine)

    dados = extract(ultimas_datas=ultimas, full_refresh=full_refresh)
    linhas_lidas = sum(len(df) for df in dados.values())

    try:
        df = transform(dados)
        n = load(df, tabela="indicadores_macro")
    except Exception as e:
        registrar_execucao(engine, "indicadores_macro", linhas_lidas, 0,
                           "erro", time.monotonic() - inicio, str(e)[:500])
        logger.exception("pipeline falhou")
        raise

    registrar_execucao(engine, "indicadores_macro", linhas_lidas, n,
                       "sucesso", time.monotonic() - inicio)
    logger.info("sucesso: %d linhas carregadas", n)
    print(df.tail())

if __name__ == "__main__":
    main()