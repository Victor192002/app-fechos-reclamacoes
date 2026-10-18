"""
app.py — Interface Streamlit para o registo de reclamações (Google Sheets).

A camada de dados vive em ``sheets_connector.py``. Este ficheiro mostra:

1. Como **inicializar** o conector (secção «1. Configuração do conector»).
2. Como usar as funções CRUD (registos, novo registo, edição).
3. Como gerir **cabeçalhos/colunas** e os **catalog dropdowns**.
4. Como expor um botão de **refresh manual** da cache.

Executar localmente::

    pip install -r requirements.txt
    # .streamlit/secrets.toml  (ou variáveis de ambiente)
    streamlit run app.py
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

import sheets_connector as sc

# ---------------------------------------------------------------------------
# Constantes da aplicação
# ---------------------------------------------------------------------------

DATA_TAB = "Reclamações"          # aba com os registos
CATALOGS_TAB = sc.DEFAULT_CATALOGS_TAB  # aba com as listas dos dropdowns

FALLBACK_OPTIONS: dict[str, list[str]] = {
    "Província": [
        "Bengo", "Benguela", "Bié", "Cabinda", "Cuando Cubango", "Cuanza Norte",
        "Cuanza Sul", "Cunene", "Huambo", "Huíla", "Luanda", "Lunda Norte",
        "Lunda Sul", "Malanje", "Moxico", "Namibe", "Uíge", "Zaire",
    ],
    "Tipo de Manifestação": [
        "Reclamação", "Denúncia", "Sugestão", "Pedido de Informação", "Elogio",
    ],
    "Caso Prorrogado": ["Não", "Sim"],
    "SLA (Prazo 30 Dias)": ["30"],
}

st.set_page_config(page_title="Gestão de Manifestações", page_icon="📋", layout="wide")


# ---------------------------------------------------------------------------
# 1. Configuração do conector  <<< ESTE É O SNIPPET DE INICIALIZAÇÃO >>>
# ---------------------------------------------------------------------------
# configure() é idempotente: se nada mudar, não invalida as caches. Por isso
# pode ser chamado em todos os reruns do Streamlit sem penalizar o desempenho.


def _secret(key: str, default=None):
    """Lê ``st.secrets`` sem rebentar quando não existe ficheiro de secrets."""
    try:
        return st.secrets.get(key, default)
    except Exception:  # StreamlitSecretNotFoundError, KeyError, ...
        return default


sc.configure(
    # O ID tem prioridade sobre o nome. Em Cloud, definir nos secrets.
    spreadsheet_id=_secret("GOOGLE_SHEETS_SPREADSHEET_ID") or None,
    spreadsheet_name=_secret("GOOGLE_SHEETS_SPREADSHEET_NAME") or "Registo de Manifestações",
    # Autenticação: st.secrets["gcp_service_account"] (Cloud) OU ficheiro local
    # OU variáveis de ambiente (GOOGLE_SERVICE_ACCOUNT_JSON / _FILE).
    credentials_info=_secret("gcp_service_account"),
    catalogs_tab=CATALOGS_TAB,
    cache_ttl=300,  # segundos; None = sem expiração automática
)

# Alternativa 100% explícita (defina SC_CREDENTIALS_FILE antes de arrancar):
# sc.configure(
#     spreadsheet_id=os.environ["SC_SPREADSHEET_ID"],
#     credentials_file=os.environ["SC_CREDENTIALS_FILE"],
# )


# ---------------------------------------------------------------------------
# 2. Barra lateral: refresh manual + diagnóstico
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("📋 Manifestações")

    if st.button("🔄 Atualizar dados da folha"):
        # Limpa st.cache_data + st.cache_resource + caches internas e recarrega.
        sc.clear_cache()
        st.rerun()

    ttl = sc.get_config().cache_ttl
    st.caption(f"Cache: {'sem expiração' if ttl is None else f'{ttl}s'} (TTL)")

    with st.expander("🩺 Diagnóstico"):
        if st.button("Testar ligação e ver abas"):
            st.json(sc.health_check())  # {ok, spreadsheet, tabs, service_account, error}
        st.caption(f"Service Account: {sc.service_account_email() or 'não detetada'}")

    analyst_filter = st.text_input(
        "Filtrar por Analista Responsável",
        value="",
        help="Deixa vazio para ver todos os registos.",
    )


# ---------------------------------------------------------------------------
# 3. Leitura (com tratamento de erros do conector)
# ---------------------------------------------------------------------------


def load_records() -> pd.DataFrame:
    """Lê a aba de dados; devolve um DataFrame vazio em caso de erro."""
    try:
        return sc.get_all_records(DATA_TAB, include_row_index=True)
    except sc.SheetNotFoundError:
        if st.button(f"Criar aba «{DATA_TAB}» com os cabeçalhos padrão"):
            sc.ensure_sheet_headers(DATA_TAB, create_tab=True)
            st.rerun()
        st.warning(f"A aba «{DATA_TAB}» ainda não existe.")
    except sc.SheetsConnectorError as exc:
        st.error(f"Erro ao ler a folha: {exc}")
    except Exception as exc:  # salvaguarda: a UI nunca deve rebentar
        st.error(f"Erro inesperado: {exc}")
    return pd.DataFrame(columns=["_sheet_row", *sc.HEADERS])


df = load_records()

if analyst_filter and not df.empty:
    try:
        df = sc.get_user_records(DATA_TAB, analyst_filter, include_row_index=True)
    except sc.SheetsConnectorError as exc:
        st.error(f"Filtro inválido: {exc}")

tab_view, tab_new, tab_edit, tab_schema, tab_catalogs = st.tabs(
    ["📄 Registos", "➕ Novo registo", "✏️ Editar", "🧱 Colunas", "⚙️ Catálogos"]
)


# ---------------------------------------------------------------------------
# 4. Vista de registos
# ---------------------------------------------------------------------------

with tab_view:
    col1, col2, col3 = st.columns(3)
    col1.metric("Registos", len(df))
    if not df.empty and sc.ANALYST_COLUMN in df.columns:
        col2.metric("Analistas distintos", df[sc.ANALYST_COLUMN].replace("", pd.NA).nunique())
    if not df.empty and "Caso Prorrogado" in df.columns:
        col3.metric("Prorrogados", int((df["Caso Prorrogado"] == "Sim").sum()))

    st.dataframe(df.drop(columns=["_sheet_row"], errors="ignore"), height=520)
    st.download_button(
        "⬇️ Exportar CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"manifestacoes_{dt.date.today():%Y-%m-%d}.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# 5. Novo registo (append)
# ---------------------------------------------------------------------------


def options_for(field: str) -> list[str]:
    """Opções do catálogo (aba «Catalogs») com fallback local."""
    try:
        values = sc.get_dropdown_options(field, CATALOGS_TAB)
    except sc.SheetsConnectorError:
        values = []
    return values or FALLBACK_OPTIONS.get(field, [])


def select_or_text(field: str, key: str, *, index: int | None = None) -> str:
    opts = options_for(field)
    if opts:
        default = index if index is not None and 0 <= index < len(opts) else 0
        return st.selectbox(field, opts, index=default, key=key)
    return st.text_input(field, key=key)


with tab_new:
    with st.form("novo_registo", clear_on_submit=True):
        left, right = st.columns(2)
        with left:
            referencia = st.text_input(
                sc.REFERENCE_COLUMN,
                value=f"REC-{dt.date.today().year}-{len(df) + 1:04d}",
            )
            data_entrada = st.date_input("Data de Entrada", value=dt.date.today())
            cliente = st.text_input("Nome do Cliente")
            contacto = st.text_input("Contacto")
            email = st.text_input("E-mail")
            provincia = select_or_text("Província", "nova_provincia")
        with right:
            tipo = select_or_text("Tipo de Manifestação", "novo_tipo")
            analista = st.text_input(sc.ANALYST_COLUMN)
            prorrogado = select_or_text("Caso Prorrogado", "novo_prorrogado")
            sla = st.number_input("SLA (Prazo 30 Dias)", min_value=0, value=30, step=1)
            data_prevista = st.date_input("Data Prevista para o Fecho", value=dt.date.today())
            matéria = st.text_area("Matéria Reclamada", height=80, key="nova_materia")
            irregularidade = st.text_area("Irregularidade Reclamada", height=80, key="nova_irregularidade")

        submitted = st.form_submit_button("Guardar registo")

    if submitted:
        try:
            result = sc.append_record(
                DATA_TAB,
                {
                    sc.REFERENCE_COLUMN: referencia,
                    "Data de Entrada": data_entrada,
                    "Nome do Cliente": cliente,
                    "Contacto": contacto,
                    "E-mail": email,
                    "Província": provincia,
                    "Tipo de Manifestação": tipo,
                    "Matéria Reclamada": matéria,
                    "Irregularidade Reclamada": irregularidade,
                    "SLA (Prazo 30 Dias)": int(sla),
                    "Caso Prorrogado": prorrogado,
                    sc.ANALYST_COLUMN: analista,
                    "Data Prevista para o Fecho": data_prevista,
                    "Número de Dias para o Fecho": "",
                },
                validate_catalogs=False,  # mude para True para impor os catálogos
            )
            st.success(f"Registo «{referencia}» guardado (linha {result['sheet_row']}).")
            sc.clear_cache()
        except sc.SchemaError as exc:
            st.error(f"A estrutura da folha não corresponde à esperada: {exc}")
        except sc.ValidationError as exc:
            st.warning(str(exc))
        except sc.SheetsConnectorError as exc:
            st.error(f"Não foi possível guardar: {exc}")


# ---------------------------------------------------------------------------
# 6. Edição (update parcial da linha)
# ---------------------------------------------------------------------------

with tab_edit:
    if df.empty:
        st.info("Sem registos para editar.")
    else:
        def _label(i: int) -> str:
            row = df.iloc[i]
            return (
                f"linha {int(row['_sheet_row'])} · {row.get(sc.REFERENCE_COLUMN, '')} · "
                f"{row.get('Nome do Cliente', '')}"
            )

        position = st.selectbox("Registo", range(len(df)), format_func=_label, key="editar_pos")
        record = df.iloc[position]

        editable = [c for c in sc.HEADERS if c != "Número de Dias para o Fecho"]
        field = st.selectbox("Campo a alterar", editable)
        current = str(record.get(field, "") or "")

        opts = options_for(field)
        if opts:
            index = opts.index(current) if current in opts else 0
            new_value: object = st.selectbox("Novo valor", ["", *opts], index=index + 1)
        elif field in sc.DATE_COLUMNS:
            parsed = pd.to_datetime(current, errors="coerce", dayfirst=True)
            new_value = st.date_input(
                "Novo valor", value=parsed.date() if pd.notna(parsed) else dt.date.today()
            )
        else:
            new_value = st.text_input("Novo valor", value=current)

        if st.button("💾 Atualizar célula"):
            try:
                cells = sc.update_record(
                    DATA_TAB,
                    int(record["_sheet_row"]),          # linha real da folha
                    {field: new_value},
                    use_sheet_row=True,
                    validate_catalogs=False,
                )
                st.success(f"{cells} célula(s) atualizada(s).")
                sc.clear_cache()
                st.rerun()
            except sc.SheetsConnectorError as exc:
                st.error(f"Falha ao atualizar: {exc}")


# ---------------------------------------------------------------------------
# 7. Gestão de colunas (cabeçalhos preservados)
# ---------------------------------------------------------------------------

with tab_schema:
    st.subheader("Colunas atuais (linha 1)")
    try:
        st.write(sc.get_sheet_headers(DATA_TAB, force_refresh=True))
    except sc.SheetsConnectorError as exc:
        st.error(str(exc))

    st.divider()
    st.subheader("Acrescentar coluna")
    with st.form("nova_coluna"):
        novo_cabecalho = st.text_input("Cabeçalho da nova coluna")
        valor_omissao = st.text_input("Valor por omissão (opcional)")
        opcoes_dropdown = st.text_area(
            "Opções do dropdown (opcional, uma por linha)",
            help="Aplica validação de dados às linhas existentes e futuras.",
        )
        criar = st.form_submit_button("Acrescentar coluna")

    if criar and novo_cabecalho:
        try:
            col = sc.add_new_column(
                DATA_TAB,
                novo_cabecalho,
                default_value=valor_omissao or None,
                dropdown_options=[o.strip() for o in opcoes_dropdown.splitlines() if o.strip()] or None,
            )
            st.success(f"Coluna «{novo_cabecalho}» criada na posição {col}.")
            sc.clear_cache()
        except sc.SheetsConnectorError as exc:
            st.error(str(exc))

    st.divider()
    with st.expander("⚠️ Administração: sincronizar linha 1 com o esquema canónico"):
        st.caption(
            "Operação sensível. Sem autorização explícita, sync_headers apenas "
            "reporta diferenças e não altera a linha 1."
        )
        allow_add = st.checkbox("Permitir acrescentar cabeçalhos em falta")
        if st.button("Analisar / sincronizar"):
            try:
                st.json(sc.sync_headers(DATA_TAB, allow_add=allow_add))
                sc.clear_cache()
            except sc.SheetsConnectorError as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# 8. Catálogos (dropdowns)
# ---------------------------------------------------------------------------

with tab_catalogs:
    st.subheader(f"Catálogos (aba «{CATALOGS_TAB}»)")
    try:
        catalogs = sc.get_all_dropdown_options(CATALOGS_TAB)
    except sc.SheetsConnectorError as exc:
        catalogs = {}
        st.warning(f"Não foi possível ler os catálogos: {exc}")

    if not catalogs:
        st.info("Ainda não existem catálogos. Crie o primeiro abaixo.")

    chosen = st.selectbox(
        "Campo", sorted(set([*sc.HEADERS, *catalogs.keys()])), key="catalog_field"
    )
    current_ops = catalogs.get(chosen, [])
    edited = st.text_area(
        "Opções (uma por linha)",
        value="\n".join(current_ops),
        height=180,
        key="catalog_options",
    )
    aplicar = sc.get_dropdown_options(chosen, CATALOGS_TAB)

    col_a, col_b = st.columns(2)
    if col_a.button("💾 Guardar opções"):
        try:
            saved = sc.update_dropdown_options(
                CATALOGS_TAB,
                chosen,
                [o.strip() for o in edited.splitlines() if o.strip()],
                mode="replace",
            )
            st.success(f"{len(saved)} opção(ões) guardada(s) para «{chosen}».")
            sc.clear_cache()
        except sc.SheetsConnectorError as exc:
            st.error(str(exc))

    if col_b.button("🪄 Aplicar catálogos como dropdowns na folha"):
        try:
            st.json(sc.apply_catalogs_to_sheet(DATA_TAB, catalog_tab_name=CATALOGS_TAB))
            st.success("Validação de dados aplicada às colunas correspondentes.")
            sc.clear_cache()
        except sc.SheetsConnectorError as exc:
            st.error(str(exc))

    if aplicar:
        st.caption(f"Opções atualmente em cache para «{chosen}»: {aplicar}")


# ---------------------------------------------------------------------------
# 9. Rodapé
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    f"sheets_connector v{sc.__version__} · aba de dados: «{DATA_TAB}» · "
    f"cabeçalho fixo na linha 1 ({len(sc.HEADERS)} colunas canónicas)"
)
