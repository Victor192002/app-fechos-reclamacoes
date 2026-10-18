# 📋 sheets_connector — Google Sheets como base de dados para Streamlit

Módulo Python (`sheets_connector.py`) que serve de **camada de acesso a dados**
para uma aplicação Streamlit de gestão de reclamações/manifestações guardadas
no Google Sheets, mais uma app de exemplo (`app.py`).

```
API Key/
├── sheets_connector.py          # data layer (gspread + google-auth)
├── app.py                       # UI Streamlit + snippet de inicialização
├── requirements.txt
├── .streamlit/
│   └── secrets.toml.example     # credenciais para Streamlit Cloud
├── tests/
│   └── test_sheets_connector.py # testes offline (folha falsa em memória)
└── README.md
```

---

## 1. Pré-requisitos no Google Cloud

1. **Ativar as APIs** no projeto (console.cloud.google.com):
   * *Google Sheets API*
   * *Google Drive API*
2. **Criar uma Service Account** → *IAM e administração* → *Service Accounts*.
3. Gerar uma **chave JSON** para essa conta e guardar o ficheiro, por exemplo,
   `service_account.json` (nunca para o Git — já está no `.gitignore`).
4. **Partilhar a folha de cálculo** com o e-mail da Service Account
   (`conector-sheets@<projeto>.iam.gserviceaccount.com`) com permissão de
   **Editor**. Sem isto a API devolve `403`/`404`.
5. Copiar o **ID da folha** a partir do URL:
   `https://docs.google.com/spreadsheets/d/<ID>/edit`.

Escopos pedidos pelo módulo (leitura/escrita total na folha e estrutura):

```python
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/drive
```

---

## 2. Instalação e execução

Requer **Python 3.9+** (Streamlit 1.32+).

```bash
pip install -r requirements.txt

# Opção A — ficheiro local
export GOOGLE_SERVICE_ACCOUNT_FILE="C:/caminho/service_account.json"   # Windows: $env:...
export GOOGLE_SHEETS_SPREADSHEET_ID="1AbC..."
streamlit run app.py

# Opção B — .streamlit/secrets.toml (recomendado)
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# preencher [gcp_service_account] e GOOGLE_SHEETS_SPREADSHEET_ID
```

No **Streamlit Community Cloud**: *Settings → Secrets* → colar o conteúdo do
`secrets.toml`. O módulo lê `st.secrets["gcp_service_account"]` e
`st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]` automaticamente.

---

## 3. Inicialização em `app.py`

```python
import streamlit as st
import sheets_connector as sc


def _secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:          # sem ficheiro de secrets
        return default


sc.configure(
    spreadsheet_id=_secret("GOOGLE_SHEETS_SPREADSHEET_ID") or None,
    spreadsheet_name="Registo de Manifestações",   # usado se o ID faltar
    credentials_info=_secret("gcp_service_account"),  # Streamlit Cloud
    # credentials_file="service_account.json",       # alternativa local
    catalogs_tab="Catalogs",
    cache_ttl=300,
)

# Botão de refresh manual (limpa st.cache_data + st.cache_resource)
if st.sidebar.button("🔄 Atualizar dados da folha"):
    sc.clear_cache()
    st.rerun()

df = sc.get_all_records("Reclamações")
st.dataframe(df)
```

> `configure()` é **idempotente**: se nada mudar, não invalida as caches. Pode
> ser chamado em cada rerun sem penalizar o desempenho.

---

## 4. Estrutura da folha (preservada, nunca alterada automaticamente)

Linha 1 da aba de dados (ordem canónica, 14 colunas):

| # | Cabeçalho | # | Cabeçalho |
|---|---|---|---|
| 1 | `Referência` | 8 | `Matéria Reclamada` |
| 2 | `Data de Entrada` | 9 | `Irregularidade Reclamada` |
| 3 | `Nome do Cliente` | 10 | `SLA (Prazo 30 Dias)` |
| 4 | `Contacto` | 11 | `Caso Prorrogado` |
| 5 | `E-mail` | 12 | `Analista Responsável` |
| 6 | `Província` | 13 | `Data Prevista para o Fecho` |
| 7 | `Tipo de Manifestação` | 14 | `Número de Dias para o Fecho` |

Regras aplicadas pelo conector:

* A **linha 1 é intocável**: só é escrita se a folha estiver totalmente vazia
  (*bootstrap*) — nunca é limpa, reordenada ou reescrita por rotina.
* Se a linha 1 não corresponder ao esquema, é lançado `SchemaError` com a lista
  de cabeçalhos em falta (o utilizador é avisado, nada é alterado).
* `add_new_column()` **acrescenta** um cabeçalho à linha 1 sem tocar nos
  restantes; `sync_headers()` é a única função que pode mexer na ordem e exige
  `allow_add=True` / `allow_reorder=True` / `allow_remove=True`.
* Mapeamento de linhas: `sheet_row = df_index + 2` (os dados começam na linha 2).

Aba de catálogos (`Catalogs`), layout «uma linha por campo»:

```
A1: Campo        B1:            C1:
A2: Província    B2: Luanda     C2: Benguela
A3: Caso Prorrogado  B3: Não    C3: Sim
```

---

## 5. API do módulo

### Leitura

| Função | Descrição |
|---|---|
| `get_all_records(sheet_name, *, force_refresh=False, parse_dates=False, include_row_index=False)` | Todos os registos como `pandas.DataFrame`. |
| `get_user_records(sheet_name, user_id, *, column="Analista Responsável", match="exact"\|"contains", include_row_index=False)` | Apenas registos do analista; **índice preservado** para edição. |
| `get_sheet_headers(sheet_name)` / `get_headers` | Linha 1, sem alterações. |
| `read_values(sheet_name)` | Valores crus (linhas × colunas) com cache. |
| `list_tabs()` | Nomes das abas do *spreadsheet*. |

### Escrita

| Função | Descrição |
|---|---|
| `append_record(sheet_name, new_row_dict, *, validate_catalogs=False, allow_unknown_fields=False)` | Acrescenta no fim; devolve `{"sheet_row", "values"}`. |
| `update_record(sheet_name, row_index, updated_dict, *, use_sheet_row=False, validate_catalogs=False)` | **Atualização parcial** por célula (`batch_update`), sem sobrescrever colunas alheias. |
| `add_new_column(sheet_name, column_header, *, position=None, default_value=None, dropdown_options=None)` | Nova coluna na linha 1, preservando dados. |
| `ensure_sheet_headers(sheet_name, *, create_tab=False)` | Admin: cria aba/garante cabeçalhos se a folha estiver vazia. |
| `sync_headers(sheet_name, *, allow_add=False, allow_reorder=False, allow_remove=False)` | Admin: alinha (por omissão apenas **reporta** diferenças). |

### Catálogos / dropdowns

| Função | Descrição |
|---|---|
| `update_dropdown_options(catalog_tab_name, field_name, new_options_list, *, mode="replace"\|"append", apply_validation_to=None)` | Grava as opções na aba de catálogos. |
| `get_dropdown_options(field_name, catalog_tab_name=None)` | Opções de um campo. |
| `get_all_dropdown_options(catalog_tab_name=None)` | `{campo: [opções]}`. |
| `apply_data_validation(sheet_name, column_header, options)` | Aplica a lista (validação de dados) à coluna. |
| `apply_catalogs_to_sheet(sheet_name, catalog_tab_name=None)` | Aplica todos os catálogos de uma vez. |

### Cache, configuração e diagnóstico

| Função | Descrição |
|---|---|
| `configure(**kwargs)` | ID/nome da folha, credenciais, aba de catálogos, TTL, retries. |
| `clear_cache()` / `refresh()` | Invalida `st.cache_data`, `st.cache_resource` e caches internas (botão «Atualizar»). |
| `health_check(refresh_client=False)` | Diagnóstico sem exceções: `{ok, spreadsheet, tabs, service_account, error}`. |
| `service_account_email()` | E-mail da SA (para a mensagem «partilhe a folha com…»). |
| `set_client(client)` | Injeta um cliente gspread (testes / autenticação externa). |

Exemplo de CRUD:

```python
df = sc.get_all_records("Reclamações", include_row_index=True)

sc.append_record("Reclamações", {
    "Referência": "REC-2026-0101",
    "Data de Entrada": "2026-10-18",
    "Nome do Cliente": "Cliente X",
    "Província": "Luanda",
    "Analista Responsável": "Ana Silva",
})

# índice do DataFrame (0-based) -> linha 2 da folha
sc.update_record("Reclamações", 0, {"Caso Prorrogado": "Sim"})
# ou pela linha real da folha
sc.update_record("Reclamações", 3, {"Província": "Bengo"}, use_sheet_row=True)

sc.add_new_column("Reclamações", "Prorrogação Aceite", dropdown_options=["Sim", "Não"])
sc.update_dropdown_options("Catalogs", "Província", ["Luanda", "Benguela", "Huíla"])
```

---

## 6. Erros, quotas e desempenho

* **Retry automático** (exponencial, com *jitter*) em `429`, `5xx`,
  `ConnectionError`, `Timeout`; por omissão 3 tentativas
  (`SHEETS_MAX_RETRIES`, `SHEETS_RETRY_BACKOFF`).
* Exceções traduzidas e acionáveis (todas derivam de `SheetsConnectorError`):

  | Exceção | Quando |
  |---|---|
  | `ConfigurationError` | Sem ID/nome da folha ou parâmetro inválido. |
  | `CredentialsError` | JSON ausente/inválido, campos em falta, auth falhada. |
  | `SpreadsheetNotFoundError` | Folha inexistente ou **não partilhada** com a SA (inclui o e-mail a partilhar). |
  | `SheetNotFoundError` | Aba inexistente (lista as abas disponíveis). |
  | `SchemaError` | Linha 1 fora do esquema; campo inexistente em `update_record`/`append_record`. |
  | `RecordNotFoundError` | Índice de linha inválido (ex.: aponta para a linha de cabeçalhos). |
  | `ValidationError` | Valor fora das opções do catálogo / parâmetro inválido. |
  | `QuotaExceededError` / `AccessDeniedError` | HTTP 429 / 401-403. |

* **Caching**: as leituras usam `@st.cache_data` (TTL de 300 s por omissão) e o
  cliente/folha usam `@st.cache_resource`. Objetos `Worksheet` e cabeçalhos
  ficam também em cache de processo para poupar chamadas à API. Qualquer
  escrita invalida a cache de dados dessa folha; o botão «🔄 Atualizar» chama
  `clear_cache()` para forçar a releitura total.
* **Atualização parcial**: `update_record` escreve apenas as células
  indicadas (1 pedido `batch_update`), reduzindo o risco de conflitos com
  edições feitas diretamente na folha.
* **Compatibilidade**: testado contra a API pública do `gspread` 5.x e 6.x
  (`batch_update` com `value_input_option`/`raw` e `add_validation`/
  `set_data_validation_for_cell_range` são detetados em tempo de execução).

---

## 7. Testes

Os testes correm **sem rede nem credenciais**, contra uma folha falsa em
memória:

```bash
python -m unittest discover -s tests -v
```

Cobrem: leitura/mapeamento de índices, filtro por analista (incluindo valores
múltiplos `"Ana Silva, Bruno Costa"`), atualização parcial, `append_record`,
`add_new_column` (com valor por omissão e dropdown), catálogos
(`replace`/`append`), validação de cabeçalhos e mensagens de erro de
configuração/credenciais.
