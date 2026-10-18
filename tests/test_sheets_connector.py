"""
tests/test_sheets_connector.py
==============================

Testes de fumaça (offline) do ``sheets_connector``: exercitam a lógica de
leitura/escrita, cabeçalhos, colunas e catálogos contra uma folha **falsa** em
memória — sem credenciais e sem rede.

Executar::

    python -m unittest discover -s tests -v
    # ou: python tests/test_sheets_connector.py
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheets_connector as sc  # noqa: E402
from gspread.exceptions import WorksheetNotFound  # noqa: E402

# Desliga as caches do Streamlit para tornar os testes determinísticos.
sc._HAS_STREAMLIT = False
for _name in ("_build_client", "_open_spreadsheet"):
    _fn = getattr(sc, _name, None)
    _inner = getattr(_fn, "__wrapped__", None)  # desembrulha st.cache_resource
    if _inner is not None:
        setattr(sc, _name, _inner)


# ---------------------------------------------------------------------------
# Folha de cálculo falsa
# ---------------------------------------------------------------------------

_A1 = re.compile(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$")
_COLS = re.compile(r"^([A-Z]+):([A-Z]+)$")


def _col_to_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


def parse_range(range_name: str) -> tuple[int, int, int, int]:
    ref = range_name.split("!")[-1]
    match = _A1.match(ref)
    if match:
        c1, r1, c2, r2 = match.groups()
        if c2 is None:
            return int(r1), _col_to_index(c1), int(r1), _col_to_index(c1)
        return int(r1), _col_to_index(c1), int(r2), _col_to_index(c2)
    full_cols = _COLS.match(ref)
    if full_cols:
        return 1, _col_to_index(full_cols.group(1)), 1000, _col_to_index(full_cols.group(2))
    raise ValueError(f"Intervalo não suportado pelo fake: {range_name!r}")


class FakeWorksheet:
    def __init__(self, title: str, rows: int = 100, cols: int = 20) -> None:
        self.title = title
        self._rows = rows
        self._cols = cols
        self._grid = [[""] * cols for _ in range(rows)]
        self.validations: dict[str, dict] = {}
        self.spreadsheet = None  # definido pelo FakeSpreadsheet
        self.id = abs(hash(title)) % 10_000

    # -- propriedades ------------------------------------------------------
    @property
    def row_count(self) -> int:
        return self._rows

    @property
    def col_count(self) -> int:
        return self._cols

    # -- helpers -----------------------------------------------------------
    def _trimmed(self) -> list[list[str]]:
        grid = [list(row) for row in self._grid]
        while grid and not any(str(c).strip() for c in grid[-1]):
            grid.pop()
        width = 0
        for row in grid:
            for i, cell in enumerate(row):
                if str(cell).strip():
                    width = max(width, i + 1)
        return [row[:width] for row in grid]

    def _ensure_shape(self, row: int, col: int) -> None:
        if row > self._rows or col > self._cols:
            raise ValueError(
                f"Range exceeds grid limits ({row} x {col} > {self._rows} x {self._cols})"
            )

    # -- API gspread -------------------------------------------------------
    def get_all_values(self) -> list[list[str]]:
        return self._trimmed()

    def row_values(self, row: int) -> list[str]:
        values = self._trimmed()
        if row - 1 >= len(values):
            return []
        out = list(values[row - 1])
        while out and not str(out[-1]).strip():
            out.pop()
        return out

    def col_values(self, col: int) -> list[str]:
        out = [row[col - 1] for row in self._trimmed() if len(row) >= col]
        while out and not str(out[-1]).strip():
            out.pop()
        return out

    def update(self, range_name=None, values=None, value_input_option="RAW", **kwargs):
        r1, c1, r2, c2 = parse_range(range_name)
        self._ensure_shape(r2, c2)
        rows = values or []
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                self._grid[r1 - 1 + i][c1 - 1 + j] = "" if value is None else str(value)
        return {"updatedCells": len(rows)}

    def update_cell(self, row: int, col: int, value) -> None:
        self._ensure_shape(row, col)
        self._grid[row - 1][col - 1] = "" if value is None else str(value)

    def batch_update(self, data, value_input_option="RAW", **kwargs):
        for entry in data:
            r1, c1, _, _ = parse_range(entry["range"])
            value = entry["values"][0][0]
            self.update_cell(r1, c1, value)
        return {"totalUpdatedCells": len(list(data))}

    def append_row(self, values, value_input_option="RAW", insert_data_option=None, **kwargs):
        table = self._trimmed()
        target = len(table) + 1  # primeira linha livre
        if len(values) > self._cols:
            raise ValueError("Range exceeds grid limits")
        for j, value in enumerate(values):
            self._grid[target - 1][j] = "" if value is None else str(value)
        last_col = sc._column_letter(max(1, len(values)))
        return {
            "updates": {
                "updatedRange": f"'{self.title}'!A{target}:{last_col}{target}",
                "updatedRows": 1,
            }
        }

    def add_cols(self, cols: int) -> None:
        for row in self._grid:
            row.extend([""] * cols)
        self._cols += cols

    def add_rows(self, rows: int) -> None:
        self._grid.extend([[""] * self._cols for _ in range(rows)])
        self._rows += rows

    def insert_cols(self, col: int, number: int = 1) -> None:
        for row in self._grid:
            for _ in range(number):
                row.insert(col - 1, "")
        self._cols += number

    def add_validation(self, range_name: str, rule: dict) -> None:
        self.validations[range_name] = rule

    # alias antigo
    set_data_validation_for_cell_range = add_validation


class FakeSpreadsheet:
    def __init__(self, title: str, sheets: list[FakeWorksheet]) -> None:
        self.title = title
        self._sheets = {ws.title: ws for ws in sheets}
        for ws in self._sheets.values():
            ws.spreadsheet = self

    def worksheet(self, title: str) -> FakeWorksheet:
        try:
            return self._sheets[title]
        except KeyError as exc:
            raise WorksheetNotFound(title) from exc

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self._sheets.values())

    def add_worksheet(self, title: str, rows: int = 100, cols: int = 20) -> FakeWorksheet:
        ws = FakeWorksheet(title, rows=rows, cols=cols)
        ws.spreadsheet = self
        self._sheets[title] = ws
        return ws


class FakeClient:
    def __init__(self, spreadsheet: FakeSpreadsheet) -> None:
        self._spreadsheet = spreadsheet

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        return self._spreadsheet

    def open(self, name: str) -> FakeSpreadsheet:
        return self._spreadsheet


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class ConnectorTestCase(unittest.TestCase):
    TAB = "Reclamações"

    def setUp(self) -> None:
        grid = FakeWorksheet(self.TAB, rows=200, cols=20)
        grid.update(range_name="A1:N1", values=[list(sc.HEADERS)])
        grid.update(
            range_name="A2:N2",
            values=[
                [
                    "REC-2026-0001", "2026-01-05", "Cliente A", "+244 923 000 001",
                    "a@exemplo.ao", "Luanda", "Reclamação", "Cobrança indevida",
                    "Fatura errada", "30", "Não", "Ana Silva", "2026-02-04", "30",
                ]
            ],
        )
        grid.update(
            range_name="A3:N3",
            values=[
                [
                    "REC-2026-0002", "2026-01-08", "Cliente B", "+244 923 000 002",
                    "b@exemplo.ao", "Benguela", "Denúncia", "Atraso na resposta",
                    "Falta de retorno", "30", "Sim", "Ana Silva, Bruno Costa",
                    "2026-02-07", "30",
                ]
            ],
        )

        self.spreadsheet = FakeSpreadsheet("Registo", [grid])
        sc.reset_configuration()
        sc.configure(spreadsheet_id="fake-id", catalogs_tab="Catalogs", cache_ttl=None)
        sc.set_client(FakeClient(self.spreadsheet))
        self.grid = grid

    def tearDown(self) -> None:
        sc.set_client(None)
        sc.reset_configuration()


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


class TestReads(ConnectorTestCase):
    def test_get_all_records(self):
        df = sc.get_all_records(self.TAB)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), list(sc.HEADERS))
        self.assertEqual(df.iloc[0]["Referência"], "REC-2026-0001")
        self.assertEqual(df.index[0], 0)

    def test_row_index_maps_to_sheet_row(self):
        df = sc.get_all_records(self.TAB, include_row_index=True)
        self.assertEqual(df["_sheet_row"].tolist(), [2, 3])
        self.assertEqual(sc.sheet_row_from_index(0), 2)
        self.assertEqual(sc.index_from_sheet_row(3), 1)

    def test_get_user_records_exact(self):
        df = sc.get_user_records(self.TAB, "Ana Silva")
        self.assertEqual(len(df), 2)  # linha com "Ana Silva, Bruno Costa" também conta
        self.assertEqual(df.index.tolist(), [0, 1])

    def test_get_user_records_multi_value_with_row_index(self):
        df = sc.get_user_records(self.TAB, "Bruno Costa", include_row_index=True)
        self.assertEqual(df.index.tolist(), [1])
        self.assertEqual(df.iloc[0]["_sheet_row"], 3)

    def test_get_user_records_contains_and_case(self):
        self.assertEqual(len(sc.get_user_records(self.TAB, "ana", match="contains")), 2)
        self.assertEqual(len(sc.get_user_records(self.TAB, "ANA SILVA", match="exact")), 2)

    def test_get_user_records_invalid_match(self):
        with self.assertRaises(sc.ValidationError):
            sc.get_user_records(self.TAB, "Ana", match="regex")

    def test_missing_tab(self):
        with self.assertRaises(sc.SheetNotFoundError):
            sc.get_all_records("Inexistente")

    def test_broken_headers_raise_schema_error(self):
        self.grid.update_cell(1, 1, "Ref")
        with self.assertRaises(sc.SchemaError):
            sc.clear_cache()
            sc.get_all_records(self.TAB)

    def test_get_sheet_headers_does_not_change_sheet(self):
        headers = sc.get_sheet_headers(self.TAB)
        self.assertEqual(headers, list(sc.HEADERS))
        self.assertEqual(self.grid.row_values(1), list(sc.HEADERS))


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------


class TestWrites(ConnectorTestCase):
    def test_update_record_by_dataframe_index(self):
        updated = sc.update_record(self.TAB, 0, {"Província": "Bengo"})
        self.assertEqual(updated, 1)
        self.assertEqual(self.grid._grid[1][5], "Bengo")  # F2
        self.assertEqual(self.grid._grid[1][0], "REC-2026-0001")  # A2 intacta

    def test_update_record_by_sheet_row(self):
        sc.update_record(self.TAB, 3, {"Caso Prorrogado": "Não"}, use_sheet_row=True)
        self.assertEqual(self.grid._grid[2][10], "Não")  # K3

    def test_update_record_multiple_fields(self):
        sc.update_record(self.TAB, 1, {"E-mail": "novo@exemplo.ao", "Contacto": "999"})
        self.assertEqual(self.grid._grid[1][3], "999")
        self.assertEqual(self.grid._grid[1][4], "novo@exemplo.ao")

    def test_update_record_unknown_field(self):
        with self.assertRaises(sc.SchemaError):
            sc.update_record(self.TAB, 0, {"Inexistente": "x"})

    def test_update_record_rejects_header_row(self):
        with self.assertRaises(sc.RecordNotFoundError):
            sc.update_record(self.TAB, 0, {"Província": "x"}, use_sheet_row=True)

    def test_update_record_serialises_dates(self):
        import datetime as dt

        sc.update_record(self.TAB, 0, {"Data Prevista para o Fecho": dt.date(2026, 3, 15)})
        self.assertEqual(self.grid._grid[1][12], "2026-03-15")

    def test_append_record(self):
        result = sc.append_record(
            self.TAB,
            {
                "Referência": "REC-2026-0003",
                "Nome do Cliente": "Cliente C",
                "Analista Responsável": "Carla Dias",
                "SLA (Prazo 30 Dias)": 30,
                "Caso Prorrogado": "Não",
            },
        )
        self.assertEqual(result["sheet_row"], 4)
        self.assertEqual(self.grid._grid[3][0], "REC-2026-0003")
        self.assertEqual(self.grid._grid[3][11], "Carla Dias")
        self.assertEqual(self.grid._grid[3][5], "")  # campos omissos ficam vazios

    def test_append_record_unknown_field(self):
        with self.assertRaises(sc.SchemaError):
            sc.append_record(self.TAB, {"Referência": "X", "Coluna Fantasma": 1})

    def test_add_new_column_appends_without_touching_existing(self):
        column = sc.add_new_column(self.TAB, "Urgente", default_value="Não")
        self.assertEqual(column, len(sc.HEADERS) + 1)
        self.assertEqual(self.grid.row_values(1)[-1], "Urgente")
        self.assertEqual(self.grid._grid[1][column - 1], "Não")
        self.assertEqual(self.grid._grid[2][column - 1], "Não")
        # cabeçalhos originais mantêm a posição
        self.assertEqual(self.grid.row_values(1)[: len(sc.HEADERS)], list(sc.HEADERS))

    def test_add_new_column_duplicate(self):
        with self.assertRaises(sc.SchemaError):
            sc.add_new_column(self.TAB, "Província")

    def test_add_new_column_with_dropdown(self):
        column = sc.add_new_column(self.TAB, "Prorrogação Aceite", dropdown_options=["Sim", "Não"])
        letter = ""
        index = column
        while index:
            index, remainder = divmod(index - 1, 26)
            letter = chr(65 + remainder) + letter
        self.assertIn(f"{letter}2:{letter}200", self.grid.validations)

    def test_append_after_new_column(self):
        sc.add_new_column(self.TAB, "Urgente")
        sc.append_record(self.TAB, {"Referência": "REC-2026-0009", "Urgente": "Sim"})
        self.assertEqual(self.grid._grid[3][len(sc.HEADERS)], "Sim")


# ---------------------------------------------------------------------------
# Catálogos / dropdowns
# ---------------------------------------------------------------------------


class TestCatalogs(ConnectorTestCase):
    def test_creates_catalogs_tab_on_demand(self):
        options = sc.update_dropdown_options("Catalogs", "Província", ["Luanda", "Benguela"])
        self.assertEqual(options, ["Luanda", "Benguela"])
        ws = self.spreadsheet.worksheet("Catalogs")
        self.assertEqual(ws.row_values(1), [sc.CATALOG_FIELD_HEADER])
        self.assertEqual(ws.row_values(2), ["Província", "Luanda", "Benguela"])
        self.assertEqual(sc.get_dropdown_options("Província", "Catalogs"), ["Luanda", "Benguela"])

    def test_replace_and_append_modes(self):
        sc.update_dropdown_options("Catalogs", "Província", ["Luanda", "Benguela"])
        sc.update_dropdown_options("Catalogs", "Província", ["Huíla"], mode="append")
        self.assertEqual(
            sc.get_dropdown_options("Província", "Catalogs"), ["Luanda", "Benguela", "Huíla"]
        )
        sc.update_dropdown_options("Catalogs", "Província", ["Cabinda"], mode="replace")
        self.assertEqual(sc.get_dropdown_options("Província", "Catalogs"), ["Cabinda"])

    def test_options_split_from_string(self):
        sc.update_dropdown_options("Catalogs", "Caso Prorrogado", "Sim; Não")
        self.assertEqual(sc.get_dropdown_options("Caso Prorrogado", "Catalogs"), ["Sim", "Não"])

    def test_get_all_dropdown_options(self):
        sc.update_dropdown_options("Catalogs", "Província", ["Luanda"])
        sc.update_dropdown_options("Catalogs", "Tipo de Manifestação", ["Reclamação", "Denúncia"])
        catalogs = sc.get_all_dropdown_options("Catalogs")
        self.assertEqual(catalogs["Província"], ["Luanda"])
        self.assertEqual(catalogs["Tipo de Manifestação"], ["Reclamação", "Denúncia"])

    def test_validate_catalogs_on_append(self):
        sc.update_dropdown_options("Catalogs", "Província", ["Luanda", "Benguela"])
        with self.assertRaises(sc.ValidationError):
            sc.append_record(self.TAB, {"Província": "Coimbra"}, validate_catalogs=True)
        sc.append_record(self.TAB, {"Província": "Benguela"}, validate_catalogs=True)
        self.assertEqual(self.grid._grid[3][5], "Benguela")

    def test_apply_catalogs_to_sheet(self):
        sc.update_dropdown_options("Catalogs", "Província", ["Luanda", "Benguela"])
        applied = sc.apply_catalogs_to_sheet(self.TAB, catalog_tab_name="Catalogs")
        self.assertIn("Província", applied)


# ---------------------------------------------------------------------------
# Configuração / erros
# ---------------------------------------------------------------------------


class TestConfigAndErrors(unittest.TestCase):
    def setUp(self):
        sc.reset_configuration()
        sc._HAS_STREAMLIT = False

    def tearDown(self):
        sc.set_client(None)
        sc.reset_configuration()

    def test_unknown_configuration_key(self):
        with self.assertRaises(sc.ConfigurationError):
            sc.configure(pasta="x")

    def test_requires_spreadsheet(self):
        sc.configure(spreadsheet_id=None, spreadsheet_name=None)
        with self.assertRaises(sc.ConfigurationError):
            sc.get_spreadsheet()

    def test_missing_credentials_message(self):
        sc.configure(credentials_file=None, credentials_info=None)
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
            self.skipTest("variáveis de ambiente de credenciais presentes")
        with self.assertRaises(sc.CredentialsError):
            sc.load_credentials()

    def test_invalid_credentials_file(self):
        with self.assertRaises(sc.CredentialsError):
            sc.configure(credentials_file="caminho/que/nao/existe.json")
            sc.load_credentials()

    def test_invalid_json_payload(self):
        with self.assertRaises(sc.CredentialsError):
            sc.configure(credentials_info="{isto não é json}")
            sc.load_credentials()

    def test_health_check_never_raises(self):
        sc.configure(spreadsheet_id=None, spreadsheet_name=None)
        report = sc.health_check()
        self.assertIn("ok", report)
        self.assertFalse(report["ok"])
        self.assertIsNotNone(report["error"])

    def test_helpers(self):
        self.assertEqual(sc._column_letter(1), "A")
        self.assertEqual(sc._column_letter(26), "Z")
        self.assertEqual(sc._column_letter(27), "AA")
        self.assertEqual(sc._norm("  Ana   SILVA "), "ana silva")
        self.assertEqual(sc._to_cell(None), "")
        self.assertEqual(sc._to_cell(True), "Sim")
        self.assertEqual(sc.sheet_row_from_index(0), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
