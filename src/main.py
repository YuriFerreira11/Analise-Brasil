from dotenv import load_dotenv
load_dotenv()

from src.pipelines.etl_macro import extract, transform, load

def main():
    dados = extract()
    df = transform(dados)
    n = load(df, tabela="indicadores_macro")
    print(f"{n} linhas carregadas em staging.indicadores_macro")
    print(df.tail())  # pra ver o resultado no terminal

if __name__ == "__main__":
    main()