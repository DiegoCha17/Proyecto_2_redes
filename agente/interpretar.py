#!/usr/bin/env python3
"""Interpretación de lenguaje natural con un proveedor de IA desacoplable (Etapa 3).

Traduce una frase del operador a una :class:`Intencion`: o bien una respuesta
conversacional, o bien la llamada a **una** herramienta declarada en
:mod:`agente.catalogo`. Este módulo no ejecuta nada: no abre sesiones SSH, no
importa Netmiko y no toca el equipo.

Seguridad:
  * Al modelo solo se le ofrecen los esquemas del catálogo (``catalogo.esquemas()``):
    no hay herramientas libres ni comandos RouterOS en el mensaje del sistema.
  * La respuesta se valida entera: nombre de herramienta dentro del catálogo,
    argumentos con las claves y tipos declarados y como mucho una acción por paso.
  * La interfaz de historial permite al agente enviar resultados filtrados;
    el agente es responsable de autorizar y minimizar esos datos.
  * Los errores del proveedor se traducen a :class:`ErrorProveedor` con un tipo
    estable y un mensaje que no reproduce el texto de la excepción (podría
    contener datos de la petición) ni la clave de API.
  * El proveedor es intercambiable (:class:`ProveedorIA`): hoy OpenAI, mañana un
    modelo local con la misma interfaz, sin tocar el resto del agente.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from agente.catalogo import Catalogo, catalogo_por_defecto
from config.ajustes import Ajustes, obtener_ajustes
from herramientas.comun import ErrorHerramienta, error_json

# --- Tipos de intención ------------------------------------------------------
TIPO_MENSAJE = "mensaje"
TIPO_HERRAMIENTA = "herramienta"

# --- Mensaje del sistema -----------------------------------------------------
# Estático a propósito: no se interpola ninguna credencial, IP de gestión ni
# salida del equipo. Describe el papel y los límites, no datos del laboratorio.
MENSAJE_SISTEMA = (
    "Eres NetAgent-IA, un asistente que ayuda a operar un laboratorio MikroTik. "
    "Respondes siempre en español, de forma breve y técnica.\n"
    "Reglas:\n"
    "1. Solo puedes proponer las herramientas del catálogo. No inventes "
    "comandos, herramientas ni propiedades que no estén declaradas.\n"
    "Las claves de equipo del laboratorio son exactamente r1 y sw1, sin "
    "guiones ni espacios; no conviertas R1 en r-1.\n"
    "2. Propón como máximo una herramienta por respuesta. Si faltan datos para "
    "usarla, pregunta al operador en vez de suponerlos.\n"
    "3. Las lecturas no modifican nada. Cualquier modificación requiere que una "
    "persona la apruebe después de ver una vista previa; tú no puedes aprobar, "
    "confirmar ni dar por hecha una modificación.\n"
    "Cambiar el comentario autorizado es descriptivo: el estado down de la "
    "interfaz no lo prohíbe. Tras un diagnóstico válido, si el operador pidió "
    "un cambio permitido con todos sus datos, propón la herramienta del catálogo; "
    "la aplicación mostrará la vista previa y solicitará la aprobación. "
    "No omitas validaciones ni supongas que el cambio fue aprobado.\n"
    "4. No afirmes que un cambio se aplicó, ni inventes estados, direcciones, "
    "comentarios o resultados. Esa información solo la aportan las herramientas.\n"
    "Si solicitan las interfaces de un estado, enumera todas las observadas con "
    "ese estado y distingue down de disabled.\n"
    "5. Nunca reveles ni solicites credenciales, claves de API, variables de "
    "entorno, direcciones IP de gestión ni volcados técnicos de los equipos.\n"
    "6. Para gestionar un bloqueo de IP usa gestionar_bloqueo solo con R1, una IPv4 "
    "LAN y accion bloquear o permitir; la aprobación siempre la da la persona. "
    "Para lo demás fuera del catálogo (reinicios, cambios de direcciones, VLAN, "
    "usuarios…), explica que no está disponible.\n"
)


class ErrorProveedor(Exception):
    """Falla controlada del proveedor de IA con un tipo estable."""

    def __init__(self, tipo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje

    def a_dict(self) -> dict[str, str]:
        """Representación serializable del error."""
        return error_json(self.tipo, self.mensaje)


@dataclass(frozen=True)
class LlamadaHerramienta:
    """Herramienta que el modelo pidió invocar, ya deserializada."""

    nombre: str
    argumentos: Mapping[str, Any]
    id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "argumentos", MappingProxyType(dict(self.argumentos)))


@dataclass(frozen=True)
class RespuestaModelo:
    """Respuesta normalizada de un proveedor: texto y llamadas solicitadas."""

    texto: str = ""
    llamadas: tuple[LlamadaHerramienta, ...] = ()


@dataclass(frozen=True)
class Intencion:
    """Lo que el agente decide hacer tras interpretar un mensaje."""

    tipo: str
    herramienta: str | None = None
    argumentos: Mapping[str, str] = MappingProxyType({})
    mensaje: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "argumentos", MappingProxyType(dict(self.argumentos)))

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable de la intención."""
        return {
            "tipo": self.tipo,
            "herramienta": self.herramienta,
            "argumentos": dict(self.argumentos),
            "mensaje": self.mensaje,
        }


@dataclass(frozen=True)
class FiltroInterfacesEstado:
    """Filtro determinista para consultas de interfaces por estado.

    El filtro no es un argumento de una herramienta: la herramienta existente
    devuelve todas las interfaces y el agente filtra únicamente los estados
    que realmente observó en esa respuesta.
    """

    estado: str
    equipos: tuple[str, ...]


@dataclass(frozen=True)
class SolicitudComentario:
    """Solicitud explícita y estructurada para cambiar un comentario."""

    equipo: str
    interfaz: str
    comentario: str


@dataclass(frozen=True)
class SolicitudAnalisis:
    """Solicitud de análisis de solo lectura para uno o varios equipos."""

    equipos: tuple[str, ...]


@dataclass(frozen=True)
class SolicitudDiagnostico:
    """Solicitud de diagnóstico factual de uno o varios equipos."""

    equipos: tuple[str, ...]


@dataclass(frozen=True)
class SolicitudInterfaces:
    """Solicitud de lectura completa de interfaces."""

    equipos: tuple[str, ...]


_PATRONES_ESTADO_INTERFACES: tuple[tuple[str, str], ...] = (
    (r"\b(?:running|conectad[oa]s?|activ[oa]s?)\b", "running"),
    (r"\b(?:down|desconectad[oa]s?|ca[ií]d[oa]s?)\b", "down"),
    (r"\b(?:disabled|desactivad[oa]s?|deshabilitad[oa]s?)\b", "disabled"),
)

_PATRON_EQUIPO = r"\b(?:r|sw)\d+\b"


def _texto_sin_acentos(texto: str) -> str:
    """Normaliza acentos solo para reconocer el vocabulario del operador."""
    normalizado = unicodedata.normalize("NFD", texto.casefold())
    return "".join(caracter for caracter in normalizado if unicodedata.category(caracter) != "Mn")


def extraer_filtro_interfaces_estado(mensaje: str) -> FiltroInterfacesEstado | None:
    """Reconoce consultas explícitas de interfaces filtradas por estado.

    Extrae claves con formato ``rN``/``swN`` y deja la comprobación de existencia
    al inventario cargado por el agente. Si no se indica un equipo, o la frase
    expresa una negación/condición ambigua, se devuelve ``None``.
    """
    if not isinstance(mensaje, str):
        return None
    texto = _texto_sin_acentos(mensaje)
    if not re.search(r"\binterfaces?\b", texto):
        return None

    equipos = tuple(dict.fromkeys(re.findall(_PATRON_EQUIPO, texto)))
    if not equipos:
        return None

    coincidencias: list[tuple[int, str]] = []
    for patron, estado in _PATRONES_ESTADO_INTERFACES:
        coincidencia = re.search(patron, texto)
        if coincidencia is None:
            continue
        antes = texto[max(0, coincidencia.start() - 18):coincidencia.start()]
        if re.search(r"\bno\s+(?:estan\s+)?$", antes):
            return None
        coincidencias.append((coincidencia.start(), estado))
    estados = {estado for _, estado in coincidencias}
    if len(estados) != 1:
        # "activas y desconectadas", por ejemplo, requiere la lista completa;
        # no se debe elegir silenciosamente solo uno de los estados.
        return None
    return FiltroInterfacesEstado(estado=coincidencias[0][1], equipos=equipos)


def extraer_solicitud_comentario(mensaje: str) -> SolicitudComentario | None:
    """Extrae el caso explícito de cambio de comentario como respaldo seguro.

    Se usa únicamente cuando el proveedor devuelve texto en vez de una llamada
    de escritura. El resultado todavía pasa por ``planificar`` y las políticas;
    esta función no autoriza ni ejecuta nada.
    """
    if not isinstance(mensaje, str):
        return None
    coincidencia = re.search(
        r"\b(?:cambia|cambiar|cambie|modifica|modificar|modifique|"
        r"actualiza|actualizar|actualice)\s+el\s+comentario\s+"
        r"(?:de|del)\s+([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s+"
        r"(?:en|de)\s+(r1|sw1)\s+(?:a|por|como)\s+(.+?)\s*[.!?]?$",
        mensaje.strip(),
        re.IGNORECASE,
    )
    if coincidencia is None:
        return None
    comentario = coincidencia.group(3).strip()
    if not comentario:
        return None
    return SolicitudComentario(
        equipo=coincidencia.group(2).casefold(),
        interfaz=coincidencia.group(1),
        comentario=comentario,
    )


def es_consulta_inventario(mensaje: str) -> bool:
    """Reconoce preguntas sobre el inventario, no sobre interfaces autorizadas."""
    if not isinstance(mensaje, str):
        return False
    texto = _texto_sin_acentos(mensaje)
    if not re.search(r"\bdispositiv(?:o|os)\b", texto):
        return False
    return bool(
        re.search(r"\binventario\b", texto)
        or re.search(r"\bdispositiv(?:o|os)\b.*\bconfigurad[oa]s?\b", texto)
    )


def extraer_solicitud_analisis(mensaje: str) -> SolicitudAnalisis | None:
    """Reconoce un análisis general de dispositivos en modo solo lectura."""
    if not isinstance(mensaje, str):
        return None
    texto = _texto_sin_acentos(mensaje)
    if not re.search(
        r"\b(?:analiza|analizar|analisis|diagnostica|diagnosticar|"
        r"revisa|revisar)\b",
        texto,
    ):
        return None
    if re.search(r"\bno\s+(?:puedes\s+)?(?:analiz|diagnostic|revis)", texto):
        return None
    equipos = tuple(dict.fromkeys(re.findall(_PATRON_EQUIPO, texto)))
    if not equipos:
        return None
    return SolicitudAnalisis(equipos=equipos)


def extraer_solicitud_diagnostico(mensaje: str) -> SolicitudDiagnostico | None:
    """Reconoce preguntas por problemas u observaciones de dispositivos."""
    if not isinstance(mensaje, str):
        return None
    texto = _texto_sin_acentos(mensaje)
    if not re.search(
        r"\b(?:problemas?|situaciones?|diagnostico|hallazgos?|"
        r"observaciones?|incidencias?|importantes?)\b",
        texto,
    ):
        return None
    equipos = tuple(dict.fromkeys(re.findall(_PATRON_EQUIPO, texto)))
    if not equipos:
        return None
    return SolicitudDiagnostico(equipos=equipos)


def extraer_solicitud_interfaces(mensaje: str) -> SolicitudInterfaces | None:
    """Reconoce una consulta completa de interfaces para equipos mencionados."""
    if not isinstance(mensaje, str):
        return None
    texto = _texto_sin_acentos(mensaje)
    if not re.search(r"\binterfaces?\b", texto):
        return None
    equipos = tuple(dict.fromkeys(re.findall(_PATRON_EQUIPO, texto)))
    if not equipos or extraer_filtro_interfaces_estado(mensaje) is not None:
        return None
    return SolicitudInterfaces(equipos=equipos)


class ProveedorIA(Protocol):
    """Contrato mínimo de un proveedor de IA con *function calling*."""

    def responder(
        self, mensaje: str, herramientas: Sequence[Mapping[str, Any]]
    ) -> RespuestaModelo:
        ...


# --- Traducción de errores del SDK ------------------------------------------
# Se clasifica por nombre de clase para no depender de una versión concreta del
# SDK y para no exponer nunca el texto original de la excepción.
_ERRORES_SDK: dict[str, tuple[str, str]] = {
    "AuthenticationError": (
        "autenticacion",
        "El proveedor de IA rechazó la clave de API; revise OPENAI_API_KEY en .env",
    ),
    "PermissionDeniedError": (
        "autenticacion",
        "La clave de API no tiene permiso para este modelo; revise la cuenta del proveedor",
    ),
    "RateLimitError": (
        "limite_tasa",
        "El proveedor de IA limitó la cantidad de solicitudes; intente de nuevo en unos segundos",
    ),
    "APITimeoutError": (
        "tiempo_agotado",
        "El proveedor de IA no respondió a tiempo; intente de nuevo",
    ),
    "APIConnectionError": (
        "conexion",
        "No se pudo conectar con el proveedor de IA; verifique la salida a Internet",
    ),
    "BadRequestError": (
        "solicitud_rechazada",
        "El proveedor de IA rechazó la solicitud (modelo o herramientas no admitidos)",
    ),
    "InternalServerError": (
        "proveedor",
        "El proveedor de IA informó un error interno; intente de nuevo más tarde",
    ),
}


def _error_de_proveedor(exc: BaseException) -> ErrorProveedor:
    """Convierte una excepción del SDK en un error tipado y sin datos sensibles."""
    for clase in type(exc).__mro__:
        traduccion = _ERRORES_SDK.get(clase.__name__)
        if traduccion is not None:
            return ErrorProveedor(*traduccion)
    return ErrorProveedor(
        "proveedor", f"Falla del proveedor de IA ({type(exc).__name__})"
    )


def _decodificar_argumentos(crudo: Any) -> dict[str, Any]:
    """Deserializa los argumentos JSON de una llamada a herramienta."""
    if crudo is None or (isinstance(crudo, str) and not crudo.strip()):
        return {}
    if not isinstance(crudo, str):
        raise ErrorProveedor(
            "respuesta_invalida",
            "El proveedor devolvió argumentos que no son JSON en texto",
        )
    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise ErrorProveedor(
            "respuesta_invalida", "El proveedor devolvió argumentos JSON mal formados"
        ) from exc
    if not isinstance(datos, dict):
        raise ErrorProveedor(
            "respuesta_invalida",
            "El proveedor devolvió argumentos que no son un objeto JSON",
        )
    return datos


def convertir_respuesta(respuesta: Any) -> RespuestaModelo:
    """Normaliza la respuesta del SDK validando cada campo que se usa.

    Se acepta un objeto con la forma de ``ChatCompletion`` (``choices`` con un
    ``message`` que puede traer ``content`` y ``tool_calls``). Cualquier forma
    inesperada se rechaza en lugar de adivinarse.
    """
    opciones = getattr(respuesta, "choices", None)
    if not opciones:
        raise ErrorProveedor(
            "respuesta_invalida", "El proveedor no devolvió ninguna respuesta"
        )

    if getattr(opciones[0], "finish_reason", None) == "length":
        raise ErrorProveedor(
            "respuesta_incompleta",
            "La respuesta del modelo alcanzó el límite de salida y es incompleta. "
            "Revise el estado antes de repetir; en Ollama puede aumentar NETAGENT_OLLAMA_MAX_TOKENS.",
        )
    mensaje = getattr(opciones[0], "message", None)
    if mensaje is None:
        raise ErrorProveedor(
            "respuesta_invalida", "El proveedor devolvió una respuesta sin mensaje"
        )

    contenido = getattr(mensaje, "content", None)
    if contenido is not None and not isinstance(contenido, str):
        raise ErrorProveedor(
            "respuesta_invalida",
            "El proveedor devolvió un contenido de mensaje no admitido",
        )

    llamadas: list[LlamadaHerramienta] = []
    for llamada in getattr(mensaje, "tool_calls", None) or ():
        funcion = getattr(llamada, "function", None)
        nombre = getattr(funcion, "name", None)
        if not isinstance(nombre, str) or not nombre.strip():
            raise ErrorProveedor(
                "respuesta_invalida",
                "El proveedor devolvió una llamada a herramienta sin nombre",
            )
        llamadas.append(
            LlamadaHerramienta(
                nombre=nombre.strip(),
                argumentos=_decodificar_argumentos(
                    getattr(funcion, "arguments", None)
                ),
                id=getattr(llamada, "id", "") or "",
            )
        )

    return RespuestaModelo(texto=(contenido or "").strip(), llamadas=tuple(llamadas))


class ProveedorOpenAI:
    """Proveedor basado en el SDK ``openai`` con *function calling*.

    ``cliente`` permite inyectar un doble en las pruebas (o un cliente apuntado
    a un servidor local compatible mediante ``base_url``), de modo que el
    agente se pueda probar sin llamadas reales.
    """

    def __init__(
        self,
        modelo: str,
        cliente: Any | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperatura: float = 0.0,
        desactivar_razonamiento: bool = False,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._modelo = modelo
        self._cliente = cliente
        self._api_key = api_key
        self._base_url = base_url
        self._temperatura = temperatura
        self._desactivar_razonamiento = desactivar_razonamiento
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_tokens = max_tokens

    @property
    def modelo(self) -> str:
        return self._modelo

    def _obtener_cliente(self) -> Any:
        """Crea el cliente del SDK la primera vez que se necesita."""
        if self._cliente is not None:
            return self._cliente

        clave = (self._api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not clave:
            raise ErrorProveedor(
                "configuracion",
                "Falta OPENAI_API_KEY: copie .env.example a .env y defina la clave "
                "del proveedor de IA",
            )

        parametros: dict[str, Any] = {"api_key": clave}
        if self._base_url:
            parametros["base_url"] = self._base_url
        if self._timeout is not None:
            parametros["timeout"] = self._timeout
        if self._max_retries is not None:
            parametros["max_retries"] = self._max_retries

        try:
            from openai import OpenAI  # importación diferida: acelera las pruebas
        except ImportError as exc:  # pragma: no cover - dependencia declarada
            raise ErrorProveedor(
                "configuracion",
                "El SDK 'openai' no está instalado; revise requirements.txt",
            ) from exc

        try:
            self._cliente = OpenAI(**parametros)
        except Exception as exc:
            raise _error_de_proveedor(exc) from exc
        return self._cliente

    def responder(
        self, mensaje: str, herramientas: Sequence[Mapping[str, Any]]
    ) -> RespuestaModelo:
        """Envía el mensaje al modelo con el catálogo y normaliza la respuesta."""
        mensajes = [
            {"role": "system", "content": MENSAJE_SISTEMA},
            {"role": "user", "content": (
                f"{mensaje}\n/no_think" if self._desactivar_razonamiento else mensaje
            )},
        ]
        return self.responder_historial(mensajes, herramientas)

    def responder_historial(
        self, mensajes: Sequence[Mapping[str, Any]], herramientas: Sequence[Mapping[str, Any]]
    ) -> RespuestaModelo:
        """Consulta con el historial OpenAI completo de una solicitud."""
        cliente = self._obtener_cliente()
        try:
            opciones: dict[str, Any] = {}
            if self._desactivar_razonamiento:
                opciones["reasoning_effort"] = "none"
            if self._max_tokens is not None:
                opciones["max_tokens"] = self._max_tokens
            respuesta = cliente.chat.completions.create(
                model=self._modelo,
                messages=list(mensajes),
                tools=list(herramientas),
                tool_choice="auto",
                temperature=self._temperatura,
                **opciones,
            )
        except Exception as exc:  # el SDK ya trae tipos; aquí se traducen
            raise _error_de_proveedor(exc) from exc
        normalizada = convertir_respuesta(respuesta)
        if self._desactivar_razonamiento:
            # Algunas versiones devuelven el razonamiento en content, incluso
            # sin la etiqueta de apertura. No lo mostramos ni lo registramos.
            texto = normalizada.texto
            if "</think>" in texto:
                texto = texto.rsplit("</think>", 1)[1].strip()
            if "<think>" in texto:
                raise ErrorProveedor("respuesta_invalida", "El modelo devolvió razonamiento sin cerrar")
            normalizada = RespuestaModelo(texto=texto, llamadas=normalizada.llamadas)
        return normalizada


def _a_intencion(respuesta: RespuestaModelo, catalogo: Catalogo) -> Intencion:
    """Valida la respuesta del modelo contra el catálogo."""
    if not respuesta.llamadas:
        return Intencion(tipo=TIPO_MENSAJE, mensaje=respuesta.texto.strip())

    if len(respuesta.llamadas) > 1:
        raise ErrorProveedor(
            "respuesta_invalida",
            "El modelo propuso más de una acción en el mismo paso; solo se admite una",
        )

    llamada = respuesta.llamadas[0]
    definicion = catalogo.obtener(llamada.nombre)  # ErrorHerramienta si no existe
    argumentos = definicion.validar_argumentos(llamada.argumentos)

    return Intencion(
        tipo=TIPO_HERRAMIENTA,
        herramienta=definicion.nombre,
        argumentos=argumentos,
        mensaje=respuesta.texto.strip(),
    )


def interpretar(
    mensaje: str,
    *,
    proveedor: ProveedorIA | None = None,
    catalogo: Catalogo | None = None,
    ajustes: Ajustes | None = None,
) -> Intencion:
    """Interpreta un mensaje del operador.

    Lanza :class:`ErrorProveedor` si el proveedor falla o responde algo inválido
    y :class:`herramientas.comun.ErrorHerramienta` si propone una herramienta o
    argumentos fuera del catálogo. Nunca ejecuta la herramienta propuesta.
    """
    if not isinstance(mensaje, str) or not mensaje.strip():
        raise ErrorHerramienta(
            "parametros_invalidos", "La consulta no puede estar vacía"
        )

    catalogo = catalogo or catalogo_por_defecto()
    if proveedor is None:
        ajustes = ajustes or obtener_ajustes()
        proveedor = crear_proveedor(ajustes)

    respuesta = proveedor.responder(mensaje.strip(), catalogo.esquemas())
    return _a_intencion(respuesta, catalogo)


def crear_proveedor(ajustes: Ajustes) -> ProveedorOpenAI:
    """Construye el proveedor configurado para una solicitud."""
    if ajustes.proveedor_ia == "ollama":
        return ProveedorOpenAI(modelo=ajustes.modelo, api_key="ollama",
                               base_url=ajustes.ollama_base_url,
                               desactivar_razonamiento=True,
                               timeout=ajustes.ollama_timeout, max_retries=0,
                               max_tokens=ajustes.ollama_max_tokens)
    return ProveedorOpenAI(modelo=ajustes.modelo)
