import time
import logging
from sqlalchemy import create_engine
import os

from dotenv import load_dotenv
load_dotenv()

from src.pipelines.etl_macro import extract, transform, load, registrar_execucao
logger = logging.getLogger("main")
def main():
    inicio = time.monotonic()
    engine = create_engine(os.environ["DATABASE_URL"])

    dados = extract()
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