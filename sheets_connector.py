"""
sheets_connector.py
===================

Camada de acesso a dados (*data layer*) para um registo de reclamações /
manifestações guardado no Google Sheets, pensada para ser consumida por uma
aplicação **Streamlit**.

Características principais
--------------------------
* Autenticação por **Service Account** (ficheiro JSON local, variável de
  ambiente ou ``st.secrets`` para Streamlit Community Cloud).
* Escopos completos de leitura/escrita: ``spreadsheets`` + ``drive``.
* Preservação da arquitetura da folha: a **linha 1 é reservada a cabeçalhos**
  e nunca é limpa, reordenada ou alterada sem uma chamada explícita de
  administração.
* CRUD: ``get_all_records``, ``get_user_records``, ``update_record``,
  ``append_record``, ``add_new_column``, ``update_dropdown_options``.
* Tabela de *catalog* (listas de valores para *dropdowns*) numa aba separada
  (por omissão ``"Catalogs"``).
* Cache ``st.cache_data`` / ``st.cache_resource`` com *refresh* manual
  (``clear_cache()`` / botão na UI) e TTL configurável.
* Tratamento de erros explícito: aba inexistente, ficheiro de credenciais
  inválido, falta de permissões, quotas/limites da API (HTTP 429) e falhas de
  rede, com *retry* exponencial automático.

Estrutura esperada da folha de dados
------------------------------------
Linha 1 (cabeçalhos fixos, ordem canónica)::

    Referência | Data de Entrada | Nome do Cliente | Contacto | E-mail |
    Província | Tipo de Manifestação | Matéria Reclamada |
    Irregularidade Reclamada | SLA (Prazo 30 Dias) | Caso Prorrogado |
    Analista Responsável | Data Prevista para o Fecho |
    Número de Dias para o Fecho

Linha 2+ -> registos. ``sheet_row = df_index + 2``.

Aba de catálogos (por omissão ``Catalogs``)::

    A1 = "Campo"  |  B1, C1, ... = rótulos auxiliares (opcional)
    A2 = "Província"            | B2 = "Luanda"   | C2 = "Benguela" | ...
    A3 = "Tipo de Manifestação" | B3 = "Reclamação" | C3 = "Sugestão" | ...

Uso rápido
----------
::

    import sheets_connector as sc

    sc.configure(spreadsheet_id="<ID>")          # ou spreadsheet_name="..."
    df = sc.get_all_records("Reclamações")       # DataFrame
    sc.append_record("Reclamações", {...})
    sc.update_record("Reclamações", 0, {"Caso Prorrogado": "Sim"})
    sc.clear_cache()                             # botão "Atualizar" da UI
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import gspread
import pandas as pd
import requests
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.service_account import Credentials
from gspread.exceptions import (
    APIError,
    GSpreadException,
    SpreadsheetNotFound,
    WorksheetNotFound,
)

try:  # Streamlit é opcional: o módulo continua a funcionar fora dele (ex.: testes).
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover - ambientes sem Streamlit
    st = None  # type: ignore[assignment]

__version__ = "1.0.0"

logger = logging.getLogger(__name__)

_HAS_STREAMLIT = st is not None


# ---------------------------------------------------------------------------
# Constantes de domínio
# ---------------------------------------------------------------------------

HEADERS: tuple[str, ...] = (
    "Referência",
    "Data de Entrada",
    "Nome do Cliente",
    "Contacto",
    "E-mail",
    "Província",
    "Tipo de Manifestação",
    "Matéria Reclamada",
    "Irregularidade Reclamada",
    "SLA (Prazo 30 Dias)",
    "Caso Prorrogado",
    "Analista Responsável",
    "Data Prevista para o Fecho",
    "Número de Dias para o Fecho",
)

HEADER_ROW = 1
FIRST_DATA_ROW = HEADER_ROW + 1  # linha 2

DATE_COLUMNS: tuple[str, ...] = ("Data de Entrada", "Data Prevista para o Fecho")
NUMERIC_COLUMNS: tuple[str, ...] = ("SLA (Prazo 30 Dias)", "Número de Dias para o Fecho")
ANALYST_COLUMN = "Analista Responsável"
REFERENCE_COLUMN = "Referência"

GOOGLE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

DEFAULT_CATALOGS_TAB = "Catalogs"
CATALOG_FIELD_HEADER = "Campo"
DEFAULT_CACHE_TTL = 300  # segundos
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.8
MULTI_VALUE_SEPARATORS = (",", ";", "|", "/")
CATALOG_VALUE_SEPARATORS = (",", ";", "|")  # sem "/" para não partir valores como "S/N"

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class SheetsConnectorError(RuntimeError):
    """Erro base do conector."""


class ConfigurationError(SheetsConnectorError):
    """Configuração em falta ou inválida (ID/nome da folha, TTL, etc.)."""


class CredentialsError(SheetsConnectorError):
    """Credenciais da Service Account ausentes, inválidas ou expiradas."""


class SpreadsheetNotFoundError(SheetsConnectorError):
    """O *spreadsheet* alvo não existe ou não foi partilhado com a SA."""


class SheetNotFoundError(SheetsConnectorError):
    """A aba (tab) pedida não existe no *spreadsheet*."""


class SchemaError(SheetsConnectorError):
    """A linha 1 não respeita o esquema esperado."""


class RecordNotFoundError(SheetsConnectorError):
    """A linha de registo indicada não existe."""


class ValidationError(SheetsConnectorError):
    """Valor de campo inválido (ex.: fora das opções do catálogo)."""


class QuotaExceededError(SheetsConnectorError):
    """Limite de utilização da API do Google excedido (HTTP 429)."""


class AccessDeniedError(SheetsConnectorError):
    """Sem permissões para a folha (HTTP 401/403)."""


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorConfig:
    """Configuração imutável do conector.

    Atributos
    ---------
    spreadsheet_id:
        Chave do *spreadsheet* (recomendado; obtém-se do URL).
    spreadsheet_name:
        Alternativa ao ID (menos robusto).
    credentials_file:
        Caminho para o JSON da Service Account.
    credentials_info:
        Dicionário com o conteúdo do JSON da Service Account
        (útil em ``st.secrets``).
    catalogs_tab:
        Nome da aba que guarda as opções dos *dropdowns*.
    cache_ttl:
        TTL do ``st.cache_data`` em segundos (``None`` = sem expiração).
    max_retries / retry_backoff:
        Política de *retry* exponencial.
    """

    spreadsheet_id: str | None = None
    spreadsheet_name: str | None = None
    credentials_file: str | None = None
    credentials_info: Mapping[str, Any] | None = None
    catalogs_tab: str = DEFAULT_CATALOGS_TAB
    cache_ttl: int | None = DEFAULT_CACHE_TTL
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _config_from_environment() -> ConnectorConfig:
    """Constrói a configuração a partir de variáveis de ambiente."""
    ttl_raw = (os.getenv("SHEETS_CACHE_TTL_SECONDS") or "").strip().lower()
    if ttl_raw in {"", "default"}:
        ttl: int | None = DEFAULT_CACHE_TTL
    elif ttl_raw in {"none", "no", "off", "-1"}:
        ttl = None
    else:
        try:
            ttl = max(0, int(float(ttl_raw)))
        except ValueError:
            logger.warning(
                "SHEETS_CACHE_TTL_SECONDS inválido (%r); a usar %s.", ttl_raw, DEFAULT_CACHE_TTL
            )
            ttl = DEFAULT_CACHE_TTL

    return ConnectorConfig(
        spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID") or None,
        spreadsheet_name=os.getenv("GOOGLE_SHEETS_SPREADSHEET_NAME") or None,
        credentials_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or None,
        catalogs_tab=os.getenv("GOOGLE_SHEETS_CATALOGS_TAB") or DEFAULT_CATALOGS_TAB,
        cache_ttl=ttl,
        max_retries=_int_env("SHEETS_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        retry_backoff=_float_env("SHEETS_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF),
    )


_CONFIG: ConnectorConfig = _config_from_environment()
_CONFIG_LOCK = threading.Lock()

_UNSET = object()


def configure(**kwargs: Any) -> ConnectorConfig:
    """Atualiza a configuração global do conector e limpa as caches.

    Apenas os argumentos fornecidos são alterados::

        sc.configure(spreadsheet_id="1AbC...", catalogs_tab="Catálogos", cache_ttl=60)

    Use ``reset_configuration()`` para voltar aos valores do ambiente.
    """
    global _CONFIG

    unknown = set(kwargs) - set(ConnectorConfig.__dataclass_fields__)
    if unknown:
        raise ConfigurationError(f"Parâmetros de configuração desconhecidos: {sorted(unknown)}")

    clean = {k: v for k, v in kwargs.items() if v is not _UNSET}
    with _CONFIG_LOCK:
        new_config = _dc_replace(_CONFIG, **clean)
        try:
            changed = new_config != _CONFIG
        except Exception:  # Mapping exótico sem __eq__ fiável
            changed = True
        _CONFIG = new_config
        config = _CONFIG

    if config.spreadsheet_id and config.spreadsheet_name:
        logger.warning(
            "Ambos 'spreadsheet_id' e 'spreadsheet_name' estão definidos; o ID tem prioridade."
        )
    # Só invalida as caches quando algo mudou: permite chamar configure() em
    # cada rerun do Streamlit sem destruir a cache de leitura.
    if changed:
        clear_cache()
    return config


def reset_configuration() -> ConnectorConfig:
    """Recarrega a configuração a partir das variáveis de ambiente."""
    global _CONFIG
    with _CONFIG_LOCK:
        _CONFIG = _config_from_environment()
        config = _CONFIG
    clear_cache()
    return config


def get_config() -> ConnectorConfig:
    """Devolve a configuração ativa."""
    return _CONFIG


def default_catalogs_tab() -> str:
    """Nome da aba de catálogos configurada."""
    return get_config().catalogs_tab


# ---------------------------------------------------------------------------
# Integração com as caches do Streamlit
# ---------------------------------------------------------------------------


def _cache_resource(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """``st.cache_resource`` quando disponível, caso contrário no-op."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if _HAS_STREAMLIT:
            try:
                return st.cache_resource(**kwargs)(func)  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                return func
        return func

    return decorator


class _LazyCache:
    """Envolve uma função em ``st.cache_data`` na primeira utilização.

    Permite alterar o TTL em tempo de execução (via ``configure()``) e
    degradar graciosamente para uma execução normal quando o Streamlit não
    está instalado.
    """

    def __init__(self, func: Callable[..., Any]) -> None:
        self._func = func
        self._wrapped: Callable[..., Any] | None = None
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not _HAS_STREAMLIT:
            return self._func(*args, **kwargs)
        wrapped = self._wrapped
        if wrapped is None:
            with self._lock:
                if self._wrapped is None:
                    try:
                        self._wrapped = st.cache_data(  # type: ignore[union-attr]
                            ttl=get_config().cache_ttl, show_spinner=False
                        )(self._func)
                    except Exception:  # pragma: no cover - Streamlit indisponível
                        self._wrapped = self._func
                wrapped = self._wrapped
        return wrapped(*args, **kwargs)  # type: ignore[misc]

    def clear(self) -> None:
        with self._lock:
            self._wrapped = None
        if _HAS_STREAMLIT:
            try:
                st.cache_data.clear()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                logger.debug("Não foi possível limpar st.cache_data.", exc_info=True)


def _resource_clear(func: Callable[..., Any]) -> None:
    clear = getattr(func, "clear", None)
    if callable(clear):
        try:
            clear()
        except Exception:  # pragma: no cover
            logger.debug("Não foi possível limpar o recurso %s.", func, exc_info=True)


# ---------------------------------------------------------------------------
# Erros: tradução e retry
# ---------------------------------------------------------------------------


def _status_code(exc: BaseException) -> int | None:
    """Extrai o código HTTP de várias formas de exceção."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    return _status_code(exc) in _RETRYABLE_STATUS


def _translate(exc: BaseException) -> BaseException:
    """Converte exceções de baixo nível em erros claros do conector."""
    if isinstance(exc, SheetsConnectorError):
        return exc

    # Preserva exceções do gspread que o chamador trata explicitamente.
    if isinstance(exc, (WorksheetNotFound, SpreadsheetNotFound)):
        return exc

    email = service_account_email()
    share_hint = f" Partilhe a folha com '{email}' (permissão de Editor)." if email else ""

    code = _status_code(exc)
    if code == 429:
        return QuotaExceededError(
            "Limite de pedidos da API do Google Sheets excedido (HTTP 429). "
            "Aguarde alguns instantes e tente novamente (as leituras estão em cache)."
        )
    if code in (401, 403):
        return AccessDeniedError(
            f"Sem permissões para aceder à folha (HTTP {code}).{share_hint}"
        )
    if code == 404:
        return SheetsConnectorError(
            f"Recurso não encontrado na API do Google (HTTP 404).{share_hint}"
        )
    if code is not None and 500 <= code < 600:
        return SheetsConnectorError(
            f"Serviço do Google Sheets temporariamente indisponível (HTTP {code}). "
            "Tente novamente dentro de momentos."
        )
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return SheetsConnectorError(f"Falha de rede ao contactar a API do Google: {exc}")
    if isinstance(exc, (GoogleAuthError, RefreshError)):
        return CredentialsError(f"As credenciais da Service Account falharam: {exc}")
    if isinstance(exc, APIError):
        return SheetsConnectorError(f"Erro da API do Google Sheets: {exc}")
    if isinstance(exc, GSpreadException):
        return SheetsConnectorError(f"Erro do gspread: {exc}")
    return exc


def call_with_retry(
    func: Callable[..., Any],
    *args: Any,
    retries: int | None = None,
    backoff: float | None = None,
    **kwargs: Any,
) -> Any:
    """Executa ``func`` com *retry* exponencial em erros transitórios.

    Erros definitivos (401/403/404, credenciais inválidas, ...) são
    traduzidos e relançados imediatamente.
    """
    cfg = get_config()
    attempts = max(0, cfg.max_retries if retries is None else int(retries))
    delay = max(0.0, cfg.retry_backoff if backoff is None else float(backoff))

    last_exc: BaseException | None = None
    for attempt in range(attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - queremos traduzir tudo
            last_exc = exc
            translated = _translate(exc)
            if attempt >= attempts or not _is_retryable(exc):
                if translated is exc:
                    raise
                raise translated from exc
            wait = delay * (2**attempt) + random.uniform(0.0, 0.25)
            logger.warning(
                "Chamada a '%s' falhou (tentativa %d/%d): %s. Nova tentativa em %.1fs.",
                getattr(func, "__qualname__", func),
                attempt + 1,
                attempts + 1,
                exc,
                wait,
            )
            time.sleep(wait)

    # Inalcançável, mas mantém o type-checker satisfeito.
    assert last_exc is not None
    translated = _translate(last_exc)
    if translated is last_exc:
        raise last_exc
    raise translated from last_exc


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------

_CLIENT_EMAIL: str | None = None
_CREDENTIALS_SOURCE: str | None = None


def _secrets_get(*keys: str) -> Any:
    """Lê a primeira chave existente em ``st.secrets`` (tolerante a falhas)."""
    if not _HAS_STREAMLIT:
        return None
    for key in keys:
        try:
            if key in st.secrets:  # type: ignore[operator]
                return st.secrets[key]  # type: ignore[index]
        except Exception:  # StreamlitSecretNotFoundError, KeyError, ...
            continue
    return None


def _normalise_service_account_blob(blob: Any) -> dict[str, Any] | None:
    """Aceita dict, string JSON ou caminho para ficheiro e devolve um dict."""
    if blob is None:
        return None
    if isinstance(blob, Mapping):
        return dict(blob)
    if isinstance(blob, (str, os.PathLike)):
        text = str(blob).strip()
        if not text:
            return None
        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CredentialsError(f"JSON de credenciais inválido: {exc}") from exc
            if not isinstance(data, dict):
                raise CredentialsError("O JSON de credenciais não contém um objeto.")
            return data
        path = Path(text).expanduser()
        if not path.is_file():
            raise CredentialsError(
                f"Ficheiro de credenciais não encontrado: '{path}'. "
                "Verifique o caminho ou use 'credentials_info' / st.secrets."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialsError(
                f"Não foi possível ler o ficheiro de credenciais '{path}': {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise CredentialsError(f"O ficheiro '{path}' não contém um objeto JSON.")
        return data
    raise CredentialsError(f"Tipo de credenciais não suportado: {type(blob)!r}")


def _resolve_service_account() -> tuple[dict[str, Any] | None, str]:
    """Descobre as credenciais por ordem de prioridade.

    Configuração explícita -> ``st.secrets`` -> variáveis de ambiente.
    """
    cfg = get_config()

    if cfg.credentials_info:
        return _normalise_service_account_blob(cfg.credentials_info), "configuração 'credentials_info'"
    if cfg.credentials_file:
        return (
            _normalise_service_account_blob(cfg.credentials_file),
            f"ficheiro '{cfg.credentials_file}'",
        )

    section = _secrets_get(
        "gcp_service_account", "google_service_account", "service_account", "GOOGLE_SERVICE_ACCOUNT"
    )
    if section is not None:
        return _normalise_service_account_blob(section), "st.secrets['gcp_service_account']"

    raw_json = _secrets_get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account_json", "SERVICE_ACCOUNT_JSON")
    if raw_json is not None:
        return _normalise_service_account_blob(raw_json), "st.secrets (JSON)"

    if _secrets_get("client_email") is not None:
        try:
            return dict(st.secrets), "st.secrets (nível raiz)"  # type: ignore[union-attr]
        except Exception:  # pragma: no cover
            pass

    for var in ("GOOGLE_SERVICE_ACCOUNT_JSON", "SERVICE_ACCOUNT_JSON", "GCP_SERVICE_ACCOUNT"):
        value = os.getenv(var)
        if value:
            return _normalise_service_account_blob(value), f"variável de ambiente {var}"
    for var in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_SERVICE_ACCOUNT_FILE", "SERVICE_ACCOUNT_FILE"):
        value = os.getenv(var)
        if value:
            return _normalise_service_account_blob(value), f"variável de ambiente {var} (ficheiro)"

    return None, "não encontrado"


def _credentials_help() -> str:
    return (
        "Credenciais da Service Account não encontradas. Forneça-as de uma das formas:\n"
        "  1. sc.configure(credentials_file='caminho/para/service_account.json')\n"
        "  2. sc.configure(credentials_info={...}) ou st.secrets['gcp_service_account'] = {...}\n"
        "  3. GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_APPLICATION_CREDENTIALS (variável de ambiente).\n"
        "Escopos necessários: " + ", ".join(GOOGLE_SCOPES)
    )


def load_credentials() -> Credentials:
    """Constrói e devolve as credenciais google-auth da Service Account."""
    global _CLIENT_EMAIL, _CREDENTIALS_SOURCE

    info, source = _resolve_service_account()
    if not info:
        raise CredentialsError(_credentials_help())

    for required in ("client_email", "private_key", "token_uri"):
        if not info.get(required):
            raise CredentialsError(
                f"As credenciais de '{source}' não contêm o campo obrigatório '{required}'."
            )

    try:
        creds = Credentials.from_service_account_info(dict(info), scopes=list(GOOGLE_SCOPES))
    except CredentialsError:
        raise
    except (ValueError, TypeError, KeyError, GoogleAuthError) as exc:
        raise CredentialsError(f"Credenciais de '{source}' inválidas: {exc}") from exc
    except Exception as exc:  # pragma: no cover - erros de criptografia/OpenSSL
        raise CredentialsError(f"Falha ao construir as credenciais de '{source}': {exc}") from exc

    _CLIENT_EMAIL = info.get("client_email")
    _CREDENTIALS_SOURCE = source
    logger.info("Credenciais da Service Account carregadas de %s.", source)
    return creds


def service_account_email() -> str | None:
    """E-mail da SA (útil na mensagem «partilhe a folha com ...»)."""
    global _CLIENT_EMAIL
    if _CLIENT_EMAIL is None:
        try:
            info, _ = _resolve_service_account()
        except SheetsConnectorError:
            return None
        if info:
            _CLIENT_EMAIL = info.get("client_email")
    return _CLIENT_EMAIL


def credentials_source() -> str | None:
    """De onde vieram as credenciais (ficheiro, st.secrets, ambiente, ...)."""
    global _CREDENTIALS_SOURCE
    if _CREDENTIALS_SOURCE is None:
        try:
            _, source = _resolve_service_account()
        except SheetsConnectorError:
            return None
        _CREDENTIALS_SOURCE = source
    return _CREDENTIALS_SOURCE


_INJECTED_CLIENT: gspread.Client | None = None


def set_client(client: gspread.Client | None) -> None:
    """Injeta um cliente já autenticado (testes ou autenticação externa)."""
    global _INJECTED_CLIENT
    _INJECTED_CLIENT = client
    clear_cache()


@_cache_resource(show_spinner=False)
def _build_client() -> gspread.Client:
    creds = load_credentials()
    try:
        return gspread.authorize(creds)
    except Exception as exc:  # noqa: BLE001
        raise CredentialsError(f"Não foi possível autenticar no Google Sheets: {exc}") from exc


def get_client() -> gspread.Client:
    """Cliente gspread autenticado (cacheado como recurso do Streamlit)."""
    if _INJECTED_CLIENT is not None:
        return _INJECTED_CLIENT
    return _build_client()


@_cache_resource(show_spinner=False)
def _open_spreadsheet() -> gspread.Spreadsheet:
    cfg = get_config()
    if not cfg.spreadsheet_id and not cfg.spreadsheet_name:
        raise ConfigurationError(
            "Nenhuma folha configurada. Chame por exemplo "
            "sc.configure(spreadsheet_id='<ID da folha>')."
        )

    client = get_client()
    email = service_account_email()
    share_hint = f" Partilhe a folha com '{email}' (Editor)." if email else ""

    try:
        if cfg.spreadsheet_id:
            return call_with_retry(client.open_by_key, cfg.spreadsheet_id.strip())
        return call_with_retry(client.open, cfg.spreadsheet_name)
    except SpreadsheetNotFound as exc:
        target = cfg.spreadsheet_id or cfg.spreadsheet_name
        raise SpreadsheetNotFoundError(
            f"Folha de cálculo '{target}' não encontrada ou não acessível à Service Account.{share_hint}"
        ) from exc
    except AccessDeniedError:
        raise
    except SheetsConnectorError as exc:
        raise SheetsConnectorError(
            f"Não foi possível abrir a folha de cálculo: {exc}{share_hint}"
        ) from exc


def get_spreadsheet() -> gspread.Spreadsheet:
    """Devolve o *spreadsheet* configurado (cacheado)."""
    return _open_spreadsheet()


# Cache em memória de objetos ``Worksheet`` e dos cabeçalhos (poupa chamadas API).
_WORKSHEETS: dict[str, gspread.Worksheet] = {}
_HEADERS_CACHE: dict[str, list[str]] = {}
_WORKSHEETS_LOCK = threading.RLock()


def _drop_structure_caches() -> None:
    with _WORKSHEETS_LOCK:
        _WORKSHEETS.clear()
        _HEADERS_CACHE.clear()


def get_worksheet(
    sheet_name: str,
    *,
    create: bool = False,
    rows: int = 1000,
    cols: int | None = None,
    initial_headers: Sequence[str] | None = None,
) -> gspread.Worksheet:
    """Devolve a aba ``sheet_name``.

    Parâmetros
    ----------
    create:
        Se ``True``, cria a aba quando não existe.
    initial_headers:
        Cabeçalhos a escrever na linha 1 da aba criada
        (por omissão, a estrutura canónica de reclamações).
    """
    if not sheet_name or not str(sheet_name).strip():
        raise SheetNotFoundError("O nome da aba não pode estar vazio.")
    key = str(sheet_name).strip()

    with _WORKSHEETS_LOCK:
        cached = _WORKSHEETS.get(key)
    if cached is not None:
        return cached

    spreadsheet = get_spreadsheet()
    created = False
    try:
        ws = call_with_retry(spreadsheet.worksheet, key)
    except WorksheetNotFound as exc:
        if not create:
            try:
                available = list_tabs()
            except SheetsConnectorError:  # pragma: no cover
                available = []
            raise SheetNotFoundError(
                f"A aba '{key}' não existe. Abas disponíveis: {available}"
            ) from exc
        headers = list(initial_headers) if initial_headers else list(HEADERS)
        logger.info("A criar a aba '%s' com os cabeçalhos padrão.", key)
        ws = call_with_retry(
            spreadsheet.add_worksheet,
            title=key,
            rows=max(int(rows), 100),
            cols=max(int(cols or 0), len(headers) + 2),
        )
        _write_header_row(ws, headers)
        created = True

    with _WORKSHEETS_LOCK:
        _WORKSHEETS[key] = ws
        if created:
            _HEADERS_CACHE[key] = list(initial_headers) if initial_headers else list(HEADERS)
    return ws


# ---------------------------------------------------------------------------
# Utilitários de valores / cabeçalhos
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    """Normaliza texto para comparações (sem acentos de caixa, espaços colapsados)."""
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").strip()
    return " ".join(text.split()).casefold()


def _clean_header(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _to_cell(value: Any) -> str:
    """Serializa um valor Python para texto seguro numa célula."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    if isinstance(value, (dt.datetime, pd.Timestamp)):
        ts = pd.Timestamp(value)
        if (ts.hour, ts.minute, ts.second, ts.microsecond) == (0, 0, 0, 0):
            return ts.strftime("%Y-%m-%d")
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(_to_cell(v)) for v in value)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _column_letter(index: int) -> str:
    """Converte um índice de coluna (1-based) em letra A1 (A, B, ..., AA)."""
    if index < 1:
        raise ValueError("O índice de coluna deve ser >= 1.")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def sheet_row_from_index(row_index: int) -> int:
    """Converte um índice de DataFrame (0-based) na linha real da folha."""
    return int(row_index) + FIRST_DATA_ROW


def index_from_sheet_row(sheet_row: int) -> int:
    """Converte uma linha real da folha no índice do DataFrame."""
    return int(sheet_row) - FIRST_DATA_ROW


def _split_multi(value: str, separators: Sequence[str] = MULTI_VALUE_SEPARATORS) -> list[str]:
    parts = [value]
    for sep in separators:
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(sep))
        parts = expanded
    return [p.strip() for p in parts if p.strip()]


def _clean_options(options: Iterable[Any] | str | None) -> list[str]:
    """Normaliza uma lista de opções: texto, sem vazios, sem duplicados."""
    if isinstance(options, str):
        options = _split_multi(options, CATALOG_VALUE_SEPARATORS)
    cleaned: list[str] = []
    for option in options or []:
        text = _to_cell(option)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _validate_headers(headers: Sequence[str], *, strict: bool = True) -> list[str]:
    """Verifica se ``headers`` contém os cabeçalhos obrigatórios."""
    present = {_norm(h) for h in headers if _norm(h)}
    missing = [h for h in HEADERS if _norm(h) not in present]
    if missing and strict:
        raise SchemaError(
            "A linha 1 da aba não contém os cabeçalhos obrigatórios: "
            + ", ".join(f"'{m}'" for m in missing)
            + ". Os cabeçalhos não são alterados automaticamente; use "
            "sync_headers(..., allow_add=True) se for intencional."
        )
    return missing


def _write_header_row(ws: gspread.Worksheet, headers: Sequence[str]) -> None:
    if len(headers) > ws.col_count:
        call_with_retry(ws.add_cols, len(headers) - ws.col_count)
    end = _column_letter(len(headers))
    call_with_retry(
        ws.update,
        range_name=f"A{HEADER_ROW}:{end}{HEADER_ROW}",
        values=[list(headers)],
        value_input_option="USER_ENTERED",
    )


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def _read_values_uncached(sheet_name: str) -> list[list[str]]:
    ws = get_worksheet(sheet_name)
    values = call_with_retry(ws.get_all_values)
    return [list(row) for row in (values or [])]


_read_values_cached = _LazyCache(_read_values_uncached)


def read_values(sheet_name: str, *, force_refresh: bool = False) -> list[list[str]]:
    """Lê todos os valores da aba (linhas x colunas) com cache.

    ``force_refresh=True`` ignora a cache e volta a ler da API.
    """
    if force_refresh or not _HAS_STREAMLIT:
        return _read_values_uncached(sheet_name)
    return _read_values_cached(sheet_name)


def get_sheet_headers(sheet_name: str, *, force_refresh: bool = False) -> list[str]:
    """Devolve a linha 1 da aba (cabeçalhos), sem a alterar."""
    key = str(sheet_name).strip()
    with _WORKSHEETS_LOCK:
        cached = _HEADERS_CACHE.get(key)
    if cached is not None and not force_refresh:
        return list(cached)

    values = read_values(key, force_refresh=force_refresh)
    headers = [_clean_header(c) for c in (values[0] if values else [])]
    while headers and headers[-1] == "":
        headers.pop()
    if not headers:
        headers = list(HEADERS)
    with _WORKSHEETS_LOCK:
        _HEADERS_CACHE[key] = list(headers)
    return headers


get_headers = get_sheet_headers  # alias público mais curto


def _build_dataframe(
    values: Sequence[Sequence[str]],
    *,
    parse_dates: bool = False,
    dayfirst: bool = True,
    validate: bool = True,
    include_row_index: bool = False,
) -> pd.DataFrame:
    if not values:
        df = pd.DataFrame(columns=list(HEADERS))
        if include_row_index:
            df.insert(0, "_sheet_row", [])
        return df

    header = [_clean_header(c) for c in values[0]]
    body = [list(r) for r in values[1:]]

    width = max([len(header)] + [len(r) for r in body] if body else [len(header)])
    header = header + [""] * (width - len(header))
    header = [name if name else f"Coluna {i + 1}" for i, name in enumerate(header)]

    # Nomes duplicados/faltosos -> torna-os únicos para o pandas.
    seen: dict[str, int] = {}
    unique_header: list[str] = []
    for name in header:
        if name in seen:
            seen[name] += 1
            unique_header.append(f"{name} ({seen[name]})")
        else:
            seen[name] = 0
            unique_header.append(name)
    header = unique_header

    if validate:
        _validate_headers(header)

    rows: list[list[str]] = []
    row_numbers: list[int] = []
    for offset, raw in enumerate(body):
        padded = list(raw[:width]) + [""] * max(0, width - len(raw))
        rows.append(padded)
        row_numbers.append(FIRST_DATA_ROW + offset)

    # Corta apenas as linhas totalmente vazias no fim. Mantém o alinhamento
    # índice do DataFrame <-> linha da folha: sheet_row = index + 2.
    last_data = -1
    for i, row in enumerate(rows):
        if any(str(cell).strip() for cell in row):
            last_data = i
    rows = rows[: last_data + 1]
    row_numbers = row_numbers[: last_data + 1]

    blank_rows = sum(1 for row in rows if not any(str(cell).strip() for cell in row))
    if blank_rows:
        logger.debug(
            "A aba contém %d linha(s) vazia(s) dentro do intervalo de dados "
            "(preservadas para manter o alinhamento das linhas).",
            blank_rows,
        )

    df = pd.DataFrame(rows, columns=header)
    df = df.apply(lambda col: col.map(lambda v: "" if v is None else str(v)))
    if include_row_index:
        df.insert(0, "_sheet_row", row_numbers)

    if parse_dates:
        for col in DATE_COLUMNS:
            if col in df.columns:
                df[col] = _parse_dates(df[col], dayfirst=dayfirst)

    return df


def _parse_dates(series: pd.Series, *, dayfirst: bool) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", dayfirst=dayfirst, format="mixed")
    except (TypeError, ValueError):
        return pd.to_datetime(series, errors="coerce", dayfirst=dayfirst)


def get_all_records(
    sheet_name: str,
    *,
    force_refresh: bool = False,
    parse_dates: bool = False,
    dayfirst: bool = True,
    validate: bool = True,
    include_row_index: bool = False,
) -> pd.DataFrame:
    """Devolve todos os registos da aba ``sheet_name`` como ``DataFrame``.

    A linha 1 é usada como cabeçalho e **não** é alterada. As colunas seguem a
    ordem da folha. O índice do DataFrame é 0-based e corresponde a
    ``sheet_row = index + 2`` — o valor a passar a ``update_record``.

    Parâmetros
    ----------
    force_refresh:
        Ignora a cache e relê da API (equivale ao botão «Atualizar»).
    parse_dates:
        Converte «Data de Entrada» e «Data Prevista para o Fecho» em datas.
    validate:
        Levanta ``SchemaError`` se faltarem cabeçalhos obrigatórios.
    include_row_index:
        Adiciona a coluna ``_sheet_row`` com a linha real da folha.
    """
    values = read_values(sheet_name, force_refresh=force_refresh)
    return _build_dataframe(
        values,
        parse_dates=parse_dates,
        dayfirst=dayfirst,
        validate=validate,
        include_row_index=include_row_index,
    )


def get_user_records(
    sheet_name: str,
    user_id: str,
    *,
    column: str = ANALYST_COLUMN,
    match: str = "exact",
    case_sensitive: bool = False,
    force_refresh: bool = False,
    parse_dates: bool = False,
    include_row_index: bool = False,
) -> pd.DataFrame:
    """Registos em que ``Analista Responsável`` corresponde a ``user_id``.

    O **índice do DataFrame é preservado**, pelo que o resultado pode ser usado
    diretamente com ``update_record`` (ou com ``use_sheet_row=True`` e
    ``include_row_index=True``).

    Parâmetros
    ----------
    match:
        ``"exact"`` (valor igual, aceitando múltiplos separados por , ; | /)
        ou ``"contains"`` (subcadeia).
    """
    df = get_all_records(
        sheet_name,
        force_refresh=force_refresh,
        parse_dates=parse_dates,
        include_row_index=include_row_index,
    )
    if match not in {"exact", "contains"}:
        raise ValidationError(f"Modo de comparação inválido: {match!r} (use 'exact' ou 'contains').")

    if df.empty or column not in df.columns:
        return df.iloc[0:0]

    target = str(user_id).strip() if case_sensitive else _norm(user_id)
    if not target:
        raise ValidationError("'user_id' não pode estar vazio.")

    def _matches(value: Any) -> bool:
        text = str(value or "")
        if match == "contains":
            haystack = text if case_sensitive else _norm(text)
            return target in haystack
        candidates = _split_multi(text) or [text]
        for candidate in candidates:
            probe = candidate if case_sensitive else _norm(candidate)
            if probe and probe == target:
                return True
        return False

    mask = df[column].map(_matches)
    return df.loc[mask]


def list_tabs() -> list[str]:
    """Devolve os nomes de todas as abas do *spreadsheet*."""
    spreadsheet = get_spreadsheet()
    worksheets = call_with_retry(spreadsheet.worksheets)
    return [ws.title for ws in worksheets]


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------


def _invalidate_data_cache() -> None:
    """Limpa a cache de leitura após qualquer escrita."""
    _read_values_cached.clear()
    with _WORKSHEETS_LOCK:
        _HEADERS_CACHE.clear()


def _ensure_sheet_ready(ws: gspread.Worksheet, sheet_name: str) -> list[str]:
    """Garante que a linha 1 tem os cabeçalhos canónicos.

    * Folha vazia -> escreve os ``HEADERS`` (bootstrap inicial).
    * Folha com dados e cabeçalhos diferentes -> ``SchemaError``. A linha 1
      nunca é alterada automaticamente.
    """
    # Lê apenas a linha 1 (barato) em vez da folha inteira.
    raw = call_with_retry(ws.row_values, HEADER_ROW) or []
    if not any(str(c).strip() for c in raw):
        logger.info("A inicializar a linha 1 da aba '%s' com os cabeçalhos padrão.", sheet_name)
        _write_header_row(ws, list(HEADERS))
        with _WORKSHEETS_LOCK:
            _HEADERS_CACHE[sheet_name] = list(HEADERS)
        return list(HEADERS)

    headers = [_clean_header(c) for c in raw]
    while headers and headers[-1] == "":
        headers.pop()
    _validate_headers(headers)
    with _WORKSHEETS_LOCK:
        _HEADERS_CACHE[sheet_name] = list(headers)
    return headers


def _header_map(ws: gspread.Worksheet, sheet_name: str) -> dict[str, int]:
    """Mapeia cabeçalho normalizado -> índice de coluna (1-based)."""
    headers = _ensure_sheet_ready(ws, sheet_name)
    mapping: dict[str, int] = {}
    for position, name in enumerate(headers, start=1):
        key = _norm(name)
        if key and key not in mapping:
            mapping[key] = position
    return mapping


def _check_against_catalogs(updates: Mapping[str, Any], *, catalogs_tab: str | None = None) -> None:
    """Valida valores contra as opções da aba de catálogos (se existirem)."""
    try:
        catalogs = get_all_dropdown_options(catalogs_tab)
    except SheetNotFoundError:
        return
    except SheetsConnectorError as exc:
        logger.warning("Não foi possível validar contra os catálogos: %s", exc)
        return

    by_norm = {_norm(field): (field, options) for field, options in catalogs.items()}
    problems: list[str] = []
    for field, value in updates.items():
        entry = by_norm.get(_norm(field))
        if not entry:
            continue
        _, options = entry
        if not options:
            continue
        text = _to_cell(value)
        if not text:
            continue
        if _norm(text) not in {_norm(o) for o in options}:
            problems.append(f"'{field}' = '{text}' (opções: {', '.join(options)})")
    if problems:
        raise ValidationError("Valores fora das opções do catálogo: " + "; ".join(problems))


def append_record(
    sheet_name: str,
    new_row_dict: Mapping[str, Any],
    *,
    validate_catalogs: bool = False,
    allow_unknown_fields: bool = False,
) -> dict[str, Any]:
    """Acrescenta um novo registo no fim da aba.

    Campos ausentes ficam vazios. Campos desconhecidos levantam
    ``SchemaError`` (ou são criados como novas colunas se
    ``allow_unknown_fields=True``).

    Devolve ``{"sheet_row": int | None, "values": [...]}``.
    """
    if not isinstance(new_row_dict, Mapping) or not new_row_dict:
        raise ValidationError("'new_row_dict' deve ser um dicionário com pelo menos um campo.")

    ws = get_worksheet(sheet_name)
    headers = _ensure_sheet_ready(ws, sheet_name)
    mapping = _header_map(ws, sheet_name)

    unknown = [k for k in new_row_dict if _norm(k) not in mapping]
    if unknown:
        if not allow_unknown_fields:
            raise SchemaError(
                "Campos inexistentes na folha: "
                + ", ".join(f"'{u}'" for u in unknown)
                + ". Use add_new_column() para os criar."
            )
        for field in unknown:
            add_new_column(sheet_name, field)
        headers = _ensure_sheet_ready(ws, sheet_name)
        mapping = _header_map(ws, sheet_name)

    if validate_catalogs:
        _check_against_catalogs(new_row_dict)

    row_values = [""] * len(headers)
    for field, value in new_row_dict.items():
        position = mapping.get(_norm(field))
        if position is None:
            continue
        row_values[position - 1] = _to_cell(value)

    if len(row_values) > ws.col_count:
        call_with_retry(ws.add_cols, len(row_values) - ws.col_count)

    result = call_with_retry(
        ws.append_row,
        row_values,
        value_input_option="USER_ENTERED",
        insert_data_option="INSERT_ROWS",
    )
    _invalidate_data_cache()

    sheet_row: int | None = None
    try:
        updated_range = ((result or {}).get("updates") or {}).get("updatedRange", "")
        match = re.search(r"![A-Z]+(\d+)", str(updated_range))
        if match:
            sheet_row = int(match.group(1))
    except Exception:  # pragma: no cover - resposta inesperada da API
        logger.debug("Não foi possível determinar a linha do novo registo.", exc_info=True)

    logger.info("Registo acrescentado à aba '%s' (linha %s).", sheet_name, sheet_row)
    return {"sheet_row": sheet_row, "values": row_values}


def update_record(
    sheet_name: str,
    row_index: int,
    updated_dict: Mapping[str, Any],
    *,
    use_sheet_row: bool = False,
    validate_catalogs: bool = False,
) -> int:
    """Atualiza células específicas de um registo (**atualização parcial**).

    Parâmetros
    ----------
    row_index:
        Índice do DataFrame (0-based) devolvido por ``get_all_records`` /
        ``get_user_records`` -> ``sheet_row = row_index + 2``.
        Com ``use_sheet_row=True``, deve passar-se a linha real (1-based).
    updated_dict:
        Mapeamento ``{cabeçalho: novo_valor}``. Só as colunas indicadas são
        tocadas, evitando sobrescrever edições concorrentes.

    Devolve o número de células atualizadas.
    """
    if not isinstance(updated_dict, Mapping) or not updated_dict:
        raise ValidationError("'updated_dict' deve ser um dicionário com pelo menos um campo.")

    try:
        sheet_row = int(row_index) if use_sheet_row else sheet_row_from_index(int(row_index))
    except (TypeError, ValueError) as exc:
        raise RecordNotFoundError(f"Índice de linha inválido: {row_index!r}") from exc
    if sheet_row < FIRST_DATA_ROW:
        raise RecordNotFoundError(
            f"A linha {sheet_row} é a linha de cabeçalhos; os dados começam na linha {FIRST_DATA_ROW}."
        )

    ws = get_worksheet(sheet_name)
    mapping = _header_map(ws, sheet_name)

    unknown = [k for k in updated_dict if _norm(k) not in mapping]
    if unknown:
        raise SchemaError(
            "Campos inexistentes na folha: "
            + ", ".join(f"'{u}'" for u in unknown)
            + ". Use add_new_column() para os criar."
        )

    if validate_catalogs:
        _check_against_catalogs(updated_dict)

    payload: list[dict[str, Any]] = []
    for field, value in updated_dict.items():
        column = mapping[_norm(field)]
        payload.append(
            {
                "range": f"{_column_letter(column)}{sheet_row}",
                "values": [[_to_cell(value)]],
            }
        )

    _batch_update(ws, payload)
    _invalidate_data_cache()
    logger.info("Linha %d da aba '%s' atualizada (%d célula(s)).", sheet_row, sheet_name, len(payload))
    return len(payload)


def _batch_update(ws: gspread.Worksheet, payload: Iterable[Mapping[str, Any]]) -> None:
    """``batch_update`` compatível com gspread 5.x e 6.x."""
    data = list(payload)

    def _call() -> Any:
        try:
            return ws.batch_update(data, value_input_option="USER_ENTERED")
        except TypeError:
            # gspread 5.x usa 'raw' em vez de 'value_input_option'.
            return ws.batch_update(data, raw=False)

    call_with_retry(_call)


def add_new_column(
    sheet_name: str,
    column_header: str,
    *,
    position: int | None = None,
    default_value: Any = None,
    dropdown_options: Sequence[str] | None = None,
) -> int:
    """Acrescenta (ou insere) uma nova coluna em Row 1, preservando os dados.

    A linha 1 é **acrescentada** (nunca reescrita por completo), pelo que as
    colunas existentes mantêm a posição e o conteúdo.

    Parâmetros
    ----------
    position:
        Índice de coluna (1-based) onde inserir; por omissão, no fim.
    default_value:
        Se fornecido, é escrito nas linhas de dados existentes.
    dropdown_options:
        Se fornecido, aplica validação de dados (lista) à coluna.

    Devolve o índice de coluna (1-based).
    """
    header = _clean_header(column_header)
    if not header:
        raise ValidationError("'column_header' não pode estar vazio.")

    ws = get_worksheet(sheet_name)
    headers = _ensure_sheet_ready(ws, sheet_name)
    existing = {_norm(h): i for i, h in enumerate(headers, start=1)}
    if _norm(header) in existing:
        raise SchemaError(f"A coluna '{header}' já existe (coluna {existing[_norm(header)]}).")

    if position is None:
        column = len(headers) + 1
    else:
        column = int(position)
        if column < 1:
            raise ValidationError("'position' deve ser >= 1.")
        if column <= len(headers):
            inserter = getattr(ws, "insert_cols", None)
            if inserter is None:  # pragma: no cover
                raise SheetsConnectorError(
                    "Esta versão do gspread não suporta inserir colunas a meio; use position=None."
                )
            call_with_retry(inserter, column, 1)

    if column + 2 > ws.col_count:
        call_with_retry(ws.add_cols, column + 2 - ws.col_count)

    call_with_retry(ws.update_cell, HEADER_ROW, column, header)

    data_rows = len(call_with_retry(ws.col_values, 1)) if default_value is not None else 0
    if default_value is not None and data_rows > HEADER_ROW:
        letter = _column_letter(column)
        try:
            call_with_retry(
                ws.update,
                range_name=f"{letter}{FIRST_DATA_ROW}:{letter}{data_rows}",
                values=[[_to_cell(default_value)]] * (data_rows - HEADER_ROW),
                value_input_option="USER_ENTERED",
            )
        except SheetsConnectorError as exc:  # não falhar a criação da coluna
            logger.warning("Coluna '%s' criada, mas o valor por omissão não foi aplicado: %s", header, exc)

    if dropdown_options:
        try:
            apply_data_validation(sheet_name, header, dropdown_options)
        except SheetsConnectorError as exc:
            logger.warning("Coluna '%s' criada sem validação de dados: %s", header, exc)

    _invalidate_data_cache()
    logger.info("Coluna '%s' criada na posição %d da aba '%s'.", header, column, sheet_name)
    return column


def ensure_sheet_headers(sheet_name: str, *, create_tab: bool = False) -> list[str]:
    """Garante os cabeçalhos padrão (função de administração).

    Escreve a linha 1 **apenas** se a folha estiver vazia. Use
    ``create_tab=True`` para criar a aba se não existir.
    """
    ws = get_worksheet(sheet_name, create=create_tab)
    headers = _ensure_sheet_ready(ws, sheet_name)
    _invalidate_data_cache()
    return headers


def sync_headers(
    sheet_name: str,
    *,
    allow_add: bool = False,
    allow_reorder: bool = False,
    allow_remove: bool = False,
) -> dict[str, list[str]]:
    """Função de administração: alinha a linha 1 com ``HEADERS``.

    Por omissão é **somente leitura** (devolve diferenças sem alterar nada).
    Cada operação destrutiva exige um *opt-in* explícito.
    """
    ws = get_worksheet(sheet_name)
    current = get_sheet_headers(sheet_name, force_refresh=True)
    current_norm = [_norm(h) for h in current]

    missing = [h for h in HEADERS if _norm(h) not in current_norm]
    extra = [h for h in current if _norm(h) and _norm(h) not in {_norm(e) for e in HEADERS}]

    if not missing and not extra:
        return {"added": [], "removed": [], "headers": current}

    if not (allow_add or allow_reorder or allow_remove):
        logger.warning(
            "sync_headers: diferenças encontradas (em falta: %s; extra: %s). "
            "Nada foi alterado — passe allow_add/allow_reorder/allow_remove explicitamente.",
            missing,
            extra,
        )
        return {"added": [], "removed": [], "missing": missing, "extra": extra, "headers": current}

    if allow_reorder:
        target = list(HEADERS) + (extra if not allow_remove else [])
    else:
        target = list(current) + (missing if allow_add else [])
        if allow_remove:
            target = [h for h in target if _norm(h) in {_norm(e) for e in HEADERS}]

    _write_header_row(ws, target)
    _invalidate_data_cache()
    removed = [h for h in current if _norm(h) not in {_norm(t) for t in target}]
    _HEADERS_CACHE[sheet_name] = list(target)
    logger.info("Cabeçalhos da aba '%s' sincronizados.", sheet_name)
    return {"added": missing if allow_add else [], "removed": removed, "headers": list(target)}


# ---------------------------------------------------------------------------
# Catálogos / dropdowns
# ---------------------------------------------------------------------------


def ensure_catalogs_tab(catalog_tab_name: str | None = None) -> gspread.Worksheet:
    """Devolve a aba de catálogos, criando-a com o cabeçalho ``Campo`` se preciso."""
    name = (catalog_tab_name or default_catalogs_tab()).strip()
    ws = get_worksheet(
        name, create=True, rows=200, cols=12, initial_headers=[CATALOG_FIELD_HEADER]
    )
    values = call_with_retry(ws.get_all_values)
    if not values or not any(str(c).strip() for c in values[0]):
        _write_header_row(ws, [CATALOG_FIELD_HEADER])
        _invalidate_data_cache()
    return ws


def get_all_dropdown_options(catalog_tab_name: str | None = None) -> dict[str, list[str]]:
    """Devolve ``{campo: [opções...]}`` a partir da aba de catálogos."""
    name = (catalog_tab_name or default_catalogs_tab()).strip()
    values = read_values(name)
    catalogs: dict[str, list[str]] = {}
    for row in values[1:]:
        if not row:
            continue
        field = _clean_header(row[0])
        if not field:
            continue
        options: list[str] = []
        for cell in row[1:]:
            text = str(cell).strip()
            if not text:
                continue
            parts = (
                _split_multi(text, CATALOG_VALUE_SEPARATORS)
                if any(sep in text for sep in CATALOG_VALUE_SEPARATORS)
                else [text]
            )
            for part in parts:
                if part and part not in options:
                    options.append(part)
        catalogs[field] = options

    if not catalogs:
        # Fallback: colunas cujo cabeçalho (linha 1) é um campo conhecido.
        header = [_clean_header(c) for c in (values[0] if values else [])]
        for index, field in enumerate(header):
            if not field or _norm(field) == _norm(CATALOG_FIELD_HEADER):
                continue
            options = [
                str(row[index]).strip()
                for row in values[1:]
                if len(row) > index and str(row[index]).strip()
            ]
            if options:
                catalogs[field] = options
    return catalogs


def get_dropdown_options(field_name: str, catalog_tab_name: str | None = None) -> list[str]:
    """Opções de um único campo (lista vazia se não existir)."""
    catalogs = get_all_dropdown_options(catalog_tab_name)
    target = _norm(field_name)
    for field, options in catalogs.items():
        if _norm(field) == target:
            return list(options)
    return []


def update_dropdown_options(
    catalog_tab_name: str,
    field_name: str,
    new_options_list: Sequence[str],
    *,
    mode: str = "replace",
    apply_validation_to: str | None = None,
) -> list[str]:
    """Cria/atualiza as opções de *dropdown* de ``field_name``.

    Estrutura usada na aba de catálogos (uma linha por campo)::

        A1 = "Campo"   B1 = ...            C1 = ...
        A2 = "Província" | B2 = "Luanda" | C2 = "Benguela"

    Parâmetros
    ----------
    mode:
        ``"replace"`` (substitui toda a lista) ou ``"append"`` (acrescenta,
        ignorando duplicados).
    apply_validation_to:
        Nome da aba de dados onde aplicar a validação de dados na coluna
        correspondente (opcional).

    Devolve a lista final de opções.
    """
    field = _clean_header(field_name)
    if not field:
        raise ValidationError("'field_name' não pode estar vazio.")

    if isinstance(new_options_list, str):
        new_options_list = _split_multi(new_options_list, CATALOG_VALUE_SEPARATORS)
    options = _clean_options(new_options_list)

    if mode not in {"replace", "append"}:
        raise ValidationError("'mode' deve ser 'replace' ou 'append'.")

    ws = ensure_catalogs_tab(catalog_tab_name)
    values = [list(row) for row in call_with_retry(ws.get_all_values)]
    if not values:
        values = [[CATALOG_FIELD_HEADER]]

    row_position: int | None = None
    for index, row in enumerate(values):
        if index == 0:
            continue
        if _clean_header(row[0] if row else "") and _norm(row[0]) == _norm(field):
            row_position = index + 1
            break

    if mode == "append" and row_position is not None:
        current = [str(c).strip() for c in values[row_position - 1][1:] if str(c).strip()]
        options = list(dict.fromkeys(current + options))

    if row_position is None:
        result_row = [field, *options]
        call_with_retry(
            ws.append_row, result_row, value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS"
        )
    else:
        row = values[row_position - 1]
        previous_width = max(len(row) - 1, len(options))
        result_row = [field] + options + [""] * (previous_width - len(options))
        end_letter = _column_letter(len(result_row))
        if len(result_row) > ws.col_count:
            call_with_retry(ws.add_cols, len(result_row) - ws.col_count)
        call_with_retry(
            ws.update,
            range_name=f"A{row_position}:{end_letter}{row_position}",
            values=[result_row],
            value_input_option="USER_ENTERED",
        )

    _invalidate_data_cache()

    if apply_validation_to:
        try:
            apply_data_validation(apply_validation_to, field, options)
        except SheetsConnectorError as exc:
            logger.warning("Opções gravadas, mas a validação de dados não foi aplicada: %s", exc)

    logger.info("Catálogo '%s' atualizado com %d opção(ões).", field, len(options))
    return options


def apply_data_validation(
    sheet_name: str,
    column_header: str,
    options: Sequence[str],
    *,
    strict: bool = False,
) -> str:
    """Aplica uma lista *dropdown* (validação de dados) a uma coluna.

    ``strict=False`` (por omissão) permite células em branco e valores
    introduzidos manualmente.

    Devolve o intervalo A1 usado (ex.: ``F2:F1000``).
    """
    clean_options = _clean_options(options)
    if not clean_options:
        raise ValidationError("É necessária pelo menos uma opção para a validação.")

    ws = get_worksheet(sheet_name)
    mapping = _header_map(ws, sheet_name)
    column = mapping.get(_norm(column_header))
    if column is None:
        raise SchemaError(f"A coluna '{column_header}' não existe na aba '{sheet_name}'.")

    last_row = max(ws.row_count, FIRST_DATA_ROW)
    letter = _column_letter(column)
    range_name = f"{letter}{FIRST_DATA_ROW}:{letter}{last_row}"

    rule: dict[str, Any] = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": option} for option in clean_options],
        },
        "showCustomUi": True,
        # strict=False permite valores em branco / introduzidos manualmente.
        "strict": bool(strict),
    }

    setter = getattr(ws, "add_validation", None) or getattr(
        ws, "set_data_validation_for_cell_range", None
    )
    if setter is None:  # pragma: no cover
        raise SheetsConnectorError(
            "A versão instalada do gspread não suporta validação de dados "
            "(atualize para gspread>=5.12)."
        )
    call_with_retry(setter, range_name, rule)
    logger.info("Validação aplicada a %s!%s (%d opções).", sheet_name, range_name, len(clean_options))
    return range_name


def apply_catalogs_to_sheet(
    sheet_name: str,
    *,
    catalog_tab_name: str | None = None,
    strict: bool = False,
) -> dict[str, str]:
    """Aplica todos os catálogos existentes às colunas correspondentes."""
    catalogs = get_all_dropdown_options(catalog_tab_name)
    headers = {_norm(h): h for h in get_sheet_headers(sheet_name)}
    applied: dict[str, str] = {}
    for field, options in catalogs.items():
        real_header = headers.get(_norm(field))
        if not real_header or not options:
            continue
        applied[real_header] = apply_data_validation(
            sheet_name, real_header, options, strict=strict
        )
    return applied


# ---------------------------------------------------------------------------
# Cache / diagnóstico
# ---------------------------------------------------------------------------


def clear_cache() -> None:
    """Limpa todas as caches (dados e recursos).

    É isto que o botão «🔄 Atualizar» da UI deve chamar, seguido de
    ``st.rerun()``.
    """
    _read_values_cached.clear()
    _drop_structure_caches()
    _resource_clear(_open_spreadsheet)
    _resource_clear(_build_client)
    logger.info("Caches do conector limpas.")


def refresh() -> None:
    """Alias de ``clear_cache()`` (semântica «recarregar da folha»)."""
    clear_cache()


def health_check(*, refresh_client: bool = False) -> dict[str, Any]:
    """Verifica credenciais, acesso à folha e devolve um resumo de diagnóstico.

    Nunca levanta exceção: o campo ``ok`` indica o resultado.
    """
    report: dict[str, Any] = {
        "ok": False,
        "spreadsheet": None,
        "tabs": [],
        "missing_headers": [],
        "service_account": None,
        "credentials_source": None,
        "cache_ttl": get_config().cache_ttl,
        "streamlit": _HAS_STREAMLIT,
        "error": None,
    }
    if refresh_client:
        clear_cache()

    try:
        report["service_account"] = service_account_email()
        report["credentials_source"] = credentials_source()
        spreadsheet = get_spreadsheet()
        report["spreadsheet"] = spreadsheet.title
        report["tabs"] = list_tabs()
        target = report["tabs"][0] if report["tabs"] else None
        if target:
            report["missing_headers"] = _validate_headers(
                get_sheet_headers(target, force_refresh=True), strict=False
            )
        report["ok"] = True
    except SheetsConnectorError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - diagnóstico nunca deve rebentar
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


# ---------------------------------------------------------------------------
# Self-test / demonstração
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = health_check()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
