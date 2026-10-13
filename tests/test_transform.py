import pandas as pd
import pandera.errors
import pytest

from src.pipelines.etl_macro import transform


def df_fake(datas: list[str], valores: list[str | None]) -> pd.DataFrame:
    """Simula a resposta da API: strings dd/mm/aaaa e valor como texto."""
    return pd.DataFrame({"data": datas, "valor": valores})


@pytest.fixture
def dados():
    """Cenário-base com TODAS as séries do config (o schema Pandera exige as 5)."""
    return {
        "taxa_selic": df_fake(
            ["02/01/2025", "15/01/2025", "03/02/2025", "20/02/2025"],
            ["10.5", "10.6", "10.65", "10.75"],
        ),
        "ipca_pct": df_fake(
            ["10/01/2025", "11/02/2025"],
            ["0.40", "0.30"],
        ),
        "cambio_usd": df_fake(
            ["05/01/2025", "20/01/2025", "05/02/2025", "20/02/2025"],
            ["5.00", "5.20", "5.10", "5.30"],
        ),
        "igpm": df_fake(
            ["30/01/2025", "28/02/2025"],
            ["0.50", "0.40"],
        ),
        "ibcbr": df_fake(
            ["15/01/2025", "15/02/2025"],
            ["90000.0", "91500.0"],
        ),
    }


def com_serie(dados, nome, valores, datas=("10/01/2025", "10/02/2025")):
    """Substitui uma série por uma de 2 meses com os valores dados."""
    dados[nome] = df_fake(list(datas), [str(v) for v in valores])
    return dados


# ---- contrato do resultado ----

def test_resultado_completo(dados):
    """Teste 'golden': confere datas, valores, ordem e dtypes de uma vez só.
    Pega bugs que testes de coluna isolada não pegam (ex.: meses deslocados)."""
    esperado = pd.DataFrame({
        "data": pd.to_datetime(["2025-01-01", "2025-02-01"]),
        "taxa_selic": [10.6, 10.75],
        "ipca_pct": [0.40, 0.30],
        "cambio_usd": [5.10, 5.20],
        "igpm": [0.50, 0.40],
        "ibcbr": [90000.0, 91500.0],
    })
    # assert_frame_equal compara floats com tolerância (não usa ==)
    pd.testing.assert_frame_equal(transform(dados), esperado)


def test_colunas_e_tipos(dados):
    df = transform(dados)
    assert list(df.columns) == [
        "data", "taxa_selic", "ipca_pct", "cambio_usd", "igpm", "ibcbr",
    ]
    assert pd.api.types.is_datetime64_any_dtype(df["data"])
    for col in df.columns.drop("data"):
        assert pd.api.types.is_float_dtype(df[col]), col


def test_datas_unicas_ordenadas_e_inicio_de_mes(dados):
    df = transform(dados)
    assert df["data"].is_unique
    assert df["data"].is_monotonic_increasing
    assert (df["data"].dt.day == 1).all()


# ---- regras de agregação ----

def test_selic_ultimo_valor_do_mes(dados):
    df = transform(dados)
    assert df["taxa_selic"].tolist() == pytest.approx([10.6, 10.75])


def test_cambio_usa_media_do_mes(dados):
    # (5.00+5.20)/2 e (5.10+5.30)/2. Float: nunca compare com ==.
    df = transform(dados)
    assert df["cambio_usd"].tolist() == pytest.approx([5.10, 5.20])


def test_join_outer_mantem_mes_com_serie_unica(dados):
    """IGP-M só tem janeiro: fevereiro continua existindo com as demais séries."""
    dados["igpm"] = df_fake(["30/01/2025"], ["0.50"])

    df = transform(dados)

    fev = df[df["data"] == "2025-02-01"].iloc[0]
    assert fev["taxa_selic"] == pytest.approx(10.75)
    assert fev["ipca_pct"] == pytest.approx(0.30)
    assert pd.isna(fev["igpm"])  # série sem fevereiro: nulo (regra do outer join)


# ---- robustez de entrada ----

def test_data_duplicada_com_valor_diferente(dados):
    """Decisão de negócio: duplicata de data -> primeira ocorrência (na ordem
    em que a API entregou) vence. Atenção: isso DEPENDE da ordem de entrada."""
    dados["taxa_selic"] = df_fake(
        ["02/01/2025", "15/01/2025", "15/01/2025", "03/02/2025"],
        ["10.5", "10.6", "99.0", "10.65"],
    )
    df = transform(dados)
    assert df["taxa_selic"].tolist() == pytest.approx([10.6, 10.65])


def test_entrada_embaralhada_da_mesmo_resultado(dados):
    """Sem duplicatas, a ordem de entrada não pode influenciar o resultado.
    O embaralhamento é gerado, então vale para qualquer série nova do config."""
    embaralhados = {
        nome: df.sample(frac=1, random_state=42).reset_index(drop=True)
        for nome, df in dados.items()
    }
    pd.testing.assert_frame_equal(transform(dados), transform(embaralhados))


def test_nao_modifica_a_entrada(dados):
    copias = {nome: df.copy() for nome, df in dados.items()}
    transform(dados)
    for nome in dados:
        pd.testing.assert_frame_equal(dados[nome], copias[nome])


def test_aceita_valores_inteiros_como_texto(dados):
    """A API pode devolver "90000" (sem casa decimal) -> deve virar float, não int."""
    dados["ibcbr"] = df_fake(["15/01/2025", "15/02/2025"], ["90000", "91500"])
    df = transform(dados)
    assert pd.api.types.is_float_dtype(df["ibcbr"])


# ---- cenários de erro (fail-fast) ----

@pytest.mark.parametrize(
    "serie, valor",
    [
        ("taxa_selic", "abc"),
        ("ipca_pct", ""),
        ("ipca_pct", None),
        ("cambio_usd", "   "),
        ("igpm", "1,5"),  # vírgula decimal: a API do BCB usa ponto
    ],
    ids=["texto", "vazio", "none", "so-espaco", "virgula-decimal"],
)
def test_rejeita_valor_invalido(dados, serie, valor):
    dados[serie] = df_fake(["10/01/2025"], [valor])
    with pytest.raises(ValueError, match=serie):
        transform(dados)


@pytest.mark.parametrize(
    "data_ruim",
    ["31/02/2025", "2025-01-10", "10-01-2025", "abc"],
    ids=["dia-inexistente", "formato-iso", "hifen", "texto"],
)
def test_rejeita_data_invalida(dados, data_ruim):
    dados["igpm"] = df_fake([data_ruim], ["0.50"])
    with pytest.raises(ValueError):
        transform(dados)


def test_rejeita_serie_vazia(dados):
    dados["ibcbr"] = df_fake([], [])
    with pytest.raises(ValueError, match="ibcbr"):
        transform(dados)


def test_rejeita_serie_ausente(dados):
    del dados["igpm"]
    with pytest.raises(pandera.errors.SchemaError):
        transform(dados)


# ---- contrato Pandera: faixas das taxas ----

@pytest.mark.parametrize(
    "serie, valor",
    [
        ("taxa_selic", -1.0), ("taxa_selic", 61.0),
        ("ipca_pct", -6.0),   ("ipca_pct", 45.0),
        ("cambio_usd", -1.0), ("cambio_usd", 21.0),
        ("igpm", -11.0),      ("igpm", 41.0),
    ],
)
def test_rejeita_taxa_fora_do_range(dados, serie, valor):
    com_serie(dados, serie, [1.0, valor])
    with pytest.raises(pandera.errors.SchemaError):
        transform(dados)


@pytest.mark.parametrize(
    "serie, valor",
    [
        ("taxa_selic", 0.0), ("taxa_selic", 60.0),
        ("ipca_pct", -5.0),  ("ipca_pct", 30.0),
        ("cambio_usd", 0.0), ("cambio_usd", 20.0),
        ("igpm", -10.0),     ("igpm", 40.0),
    ],
)
def test_aceita_taxa_exatamente_no_limite(dados, serie, valor):
    """Fronteira: o limite em si é válido (pega erro de < vs <=)."""
    com_serie(dados, serie, [1.0, valor])
    transform(dados)  # não deve levantar


# ---- contrato Pandera: nível do IBC-Br ----

def test_rejeita_ibcbr_negativo(dados):
    com_serie(dados, "ibcbr", [90000.0, -5.0])
    with pytest.raises(pandera.errors.SchemaError):
        transform(dados)


def test_rejeita_ibcbr_com_salto_maior_que_50_pct(dados):
    com_serie(dados, "ibcbr", [90000.0, 200000.0])  # +122%
    with pytest.raises(pandera.errors.SchemaError, match="50%"):
        transform(dados)


def test_aceita_ibcbr_com_variacao_abaixo_de_50_pct(dados):
    com_serie(dados, "ibcbr", [90000.0, 130000.0])  # +44%
    transform(dados)


def test_aceita_ibcbr_com_um_unico_mes(dados):
    """Com um só ponto não há variação a medir: não é erro de dado."""
    dados["ibcbr"] = df_fake(["15/01/2025"], ["90000.0"])
    df = transform(dados)
    assert df["ibcbr"].notna().sum() == 1