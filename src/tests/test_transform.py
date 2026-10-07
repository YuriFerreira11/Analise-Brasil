import pandas as pd
import pytest
import pandera.errors
from src.pipelines.etl_macro import transform


def df_fake(datas: list[str], valores: list[str]) -> pd.DataFrame:
    """Simula a resposta da API: strings dd/mm/aaaa e valor como texto."""
    return pd.DataFrame({"data": datas, "valor": valores})


@pytest.fixture
def dados():
    return {
        "selic": df_fake(
            ["02/01/2025", "15/01/2025", "03/02/2025", "20/02/2025"],
            ["10.5", "10.6", "10.65", "10.75"],
        ),
        "ipca": df_fake(
            ["10/01/2025", "11/02/2025"],
            ["0.40", "0.30"],
        ),
    }


def test_colunas(dados):
    df = transform(dados)
    assert list(df.columns) == ["data", "taxa_selic", "ipca_pct"]
    assert pd.api.types.is_datetime64_any_dtype(df["data"])
    assert pd.api.types.is_float_dtype(df["taxa_selic"])
    assert pd.api.types.is_float_dtype(df["ipca_pct"])


def test_selic_ultimo_valor_do_mes(dados):
    # Valores hardcoded: o contrato é "último do mês", ponto.
    df = transform(dados)
    assert list(df["taxa_selic"]) == [10.6, 10.75]


def test_mes_presente_em_ambas_as_series(dados):
    ipca = dados["ipca"]
    dados["ipca"] = df_fake(
        list(ipca["data"][:-1]), list(ipca["valor"][:-1])
    )
    df = transform(dados)

    def meses(df_api):
        s = pd.to_datetime(df_api["data"], format="%d/%m/%Y")
        return set(s.dt.to_period("M"))

    assert set(df["data"].dt.to_period("M")) == (
        meses(dados["selic"]) & meses(dados["ipca"])
    )


def test_data_duplicada_com_valor_diferente(dados):
    """Decisão de negócio documentada: duplicata de data -> primeira ocorrência vence."""
    dados["selic"] = df_fake(
        ["02/01/2025", "15/01/2025", "15/01/2025", "03/02/2025"],
        ["10.5", "10.6", "99.0", "10.65"],
    )
    df = transform(dados)
    assert list(df["taxa_selic"]) == [10.6, 10.65]  # 99.0 nunca pode vencer


def test_entrada_embaralhada_da_mesmo_resultado(dados):
    """Ordem de entrada não pode influenciar o resultado.
    Se o sort_values sumir, a ordem adversária abaixo pega o valor errado."""
    dados_embaralhados = {
        "selic": df_fake(
            ["15/01/2025", "02/01/2025", "20/02/2025", "03/02/2025"],  # ordem adversária
            ["10.6", "10.5", "10.75", "10.65"],
        ),
        "ipca": dados["ipca"].iloc[::-1].reset_index(drop=True),
    }
    pd.testing.assert_frame_equal(transform(dados), transform(dados_embaralhados))


# ---- cenários de erro (fail-fast) ----

def test_rejeita_nao_numerico(dados):
    dados["selic"] = df_fake(["02/01/2025"], ["abc"])
    with pytest.raises(ValueError, match="taxa_selic"):
        transform(dados)


def test_rejeita_vazio(dados):
    dados["ipca"] = df_fake(["10/01/2025"], [""])
    with pytest.raises(ValueError, match="ipca_pct"):
        transform(dados)


def test_rejeita_nulo(dados):
    dados["ipca"] = df_fake(["10/01/2025"], [None])
    with pytest.raises(ValueError, match="ipca_pct"):
        transform(dados)
def test_rejeita_selic_negativa(dados):
    dados["selic"] = df_fake(["02/01/2025", "11/02/2025"], ["-1.0", "10.75"])
    with pytest.raises(pandera.errors.SchemaError):
        transform(dados)

def test_rejeita_ipca_fora_do_range(dados):
    dados["ipca"] = df_fake(["10/01/2025", "11/02/2025"], ["0.40", "45.0"])
    with pytest.raises(pandera.errors.SchemaError):
        transform(dados)