"""Utilidades comunes de las herramientas del catálogo NetAgent-IA.

Esta capa concentra todo el acceso a los MikroTik para que las herramientas
(``estado_interfaces.py``, ``actualizar_comentario.py``, …) no manipulen
Netmiko directamente y no puedan ejecutar comandos arbitrarios.

Principios:
  * Las credenciales y las IPs provienen de :mod:`config.ajustes` (``.env`` +
    ``config/dispositivos.yml``); nunca se escriben en el código.
  * Solo se ejecutan comandos de un catálogo cerrado (:data:`COMANDOS_LECTURA`).
    Un texto recibido del modelo de IA no llega nunca a la sesión SSH.
  * La única escritura permitida es el comentario de una interfaz, siempre
    construida aquí a partir de parámetros ya validados; no existe una función
    que acepte un comando RouterOS arbitrario.
  * Toda falla se traduce a :class:`ErrorHerramienta` con un tipo estable, sin
    trazas ni contraseñas, para que el agente reciba siempre JSON.
  * Las respuestas usan un sobre único (:func:`construir_respuesta`).
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
    ReadTimeout,
)

from config.ajustes import Ajustes, ErrorConfiguracion, obtener_ajustes

logger = logging.getLogger(__name__)

# --- Tiempos de espera (segundos) -------------------------------------------
# Valores holgados: los MikroTik virtualizados en GNS3 responden con latencia.
TIMEOUT_CONEXION = 20.0
TIMEOUT_LECTURA = 45.0
# Silencio que se espera tras una escritura antes de dar la respuesta por
# terminada (lectura por tiempo, ver ``ejecutar_modificacion``).
ESPERA_SILENCIO = 1.0

# --- Catálogo cerrado de comandos -------------------------------------------
COMANDO_INTERFACES = "/interface print detail without-paging"
COMANDO_DIRECCIONES = "/ip address print detail without-paging"

COMANDOS_LECTURA = frozenset({COMANDO_INTERFACES, COMANDO_DIRECCIONES})

# --- Única escritura autorizada ---------------------------------------------
# Se cambia exclusivamente el comentario de una interfaz ya autorizada por
# ``config/dispositivos.yml``. El comando no se recibe de fuera: se construye
# aquí tras validar el nombre y el texto, de modo que no pueda inyectarse otra
# sentencia RouterOS (comillas, saltos de línea, ``$`` o ``;``).
MAX_COMENTARIO = 200
_PATRON_INTERFAZ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATRON_COMENTARIO = re.compile(r'^[^"\\$\x00-\x1f\x7f]{0,200}$')
# Mensajes con los que RouterOS rechaza una sentencia. Se busca al inicio de
# línea (``re.M``) para no confundirlos con el eco del comando ni con texto de
# un comentario. Un ``set`` cuyo ``[find]`` no casa nada no imprime nada, así
# que la ausencia de error nunca se interpreta como éxito: la autoridad es la
# relectura posterior.
_ERROR_ROUTEROS = re.compile(
    r"(?im)^[ \t]*(?:failure:|bad command name|syntax error"
    r"|expected end of command|no such item|input does not match"
    r"|unknown parameter|cannot find|not enough arguments|action failed"
    r"|interrupted)(?:\s|$)"
)


class ErrorHerramienta(Exception):
    """Falla controlada con un tipo estable para las respuestas JSON.

    ``respuesta_equipo`` conserva, cuando existe, la salida cruda del equipo
    que provocó el error, para poder diagnosticar un rechazo de RouterOS sin
    tener que repetir la operación.
    """

    def __init__(
        self, tipo: str, mensaje: str, respuesta_equipo: str | None = None
    ) -> None:
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje
        self.respuesta_equipo = respuesta_equipo

    def a_dict(self) -> dict[str, str]:
        """Representación serializable del error."""
        return error_json(self.tipo, self.mensaje)


def error_json(tipo: str, mensaje: str) -> dict[str, str]:
    """Construye el objeto de error usado en todas las respuestas."""
    return {"tipo": tipo, "mensaje": mensaje}


def construir_respuesta(
    equipo: str,
    interfaces: list[dict[str, Any]] | None = None,
    error: Mapping[str, str] | None = None,
    changed: bool = False,
) -> dict[str, Any]:
    """Devuelve el sobre JSON común a todas las herramientas.

    ``ok`` es verdadero solo si no hay error. ``changed`` queda siempre en
    ``False`` para herramientas de lectura.
    """
    return {
        "ok": error is None,
        "equipo": equipo,
        "interfaces": list(interfaces or []),
        "changed": bool(changed),
        "error": dict(error) if error is not None else None,
    }


def imprimir_json(respuesta: Mapping[str, Any]) -> None:
    """Escribe la respuesta JSON en stdout (única salida de las herramientas)."""
    print(json.dumps(respuesta, ensure_ascii=False, indent=2))


def _sanitizar(texto: str, secreto: str) -> str:
    """Elimina la contraseña de un texto antes de mostrarlo o registrarlo."""
    if secreto:
        texto = texto.replace(secreto, "***")
    return texto


def _resumen_excepcion(exc: BaseException) -> str:
    """Resumen corto y sin credenciales de una excepción inesperada."""
    detalle = str(exc).strip().splitlines()
    primero = detalle[0][:200] if detalle else ""
    return f"{type(exc).__name__}: {primero}" if primero else type(exc).__name__


def abrir_conexion(clave: str, ajustes: Ajustes | None = None) -> Any:
    """Abre una sesión SSH con un dispositivo autorizado.

    Lanza :class:`config.ajustes.ErrorConfiguracion` si el equipo no existe o
    faltan credenciales, y :class:`ErrorHerramienta` si Netmiko falla.
    """
    ajustes = ajustes or obtener_ajustes()
    dispositivo = ajustes.dispositivo(clave)
    credenciales = ajustes.credenciales(clave)

    try:
        return ConnectHandler(
            device_type=dispositivo.plataforma,
            host=dispositivo.host,
            port=credenciales.puerto,
            username=credenciales.usuario,
            password=credenciales.clave,
            conn_timeout=TIMEOUT_CONEXION,
            banner_timeout=TIMEOUT_CONEXION,
            auth_timeout=TIMEOUT_CONEXION,
            fast_cli=False,
            use_keys=False,
            allow_agent=False,
        )
    except NetmikoAuthenticationException as exc:
        raise ErrorHerramienta(
            "autenticacion",
            f"Autenticación rechazada por {dispositivo.nombre} "
            f"({dispositivo.host}); revise {dispositivo.usuario_env} y "
            f"{dispositivo.clave_env}",
        ) from exc
    except (NetmikoTimeoutException, ReadTimeout) as exc:
        raise ErrorHerramienta(
            "tiempo_agotado",
            f"No se pudo conectar con {dispositivo.nombre} "
            f"({dispositivo.host}:{credenciales.puerto}) en "
            f"{TIMEOUT_CONEXION:.0f}s; verifique que GNS3 esté iniciado",
        ) from exc
    except Exception as exc:  # red, SSH, algoritmo, driver, etc.
        detalle = _sanitizar(_resumen_excepcion(exc), credenciales.clave)
        raise ErrorHerramienta(
            "conexion",
            f"Error al conectar con {dispositivo.nombre} "
            f"({dispositivo.host}:{credenciales.puerto}): {detalle}",
        ) from exc


def cerrar_conexion(conexion: Any | None) -> None:
    """Cierra la sesión SSH ignorando errores de cierre."""
    if conexion is None:
        return
    try:
        conexion.disconnect()
    except Exception:  # un fallo al cerrar no debe invalidar la lectura
        logger.debug("Fallo al cerrar la sesión SSH", exc_info=False)


@contextmanager
def conexion_dispositivo(
    clave: str, ajustes: Ajustes | None = None
) -> Iterator[Any]:
    """Context manager que garantiza el cierre de la sesión SSH."""
    conexion = abrir_conexion(clave, ajustes=ajustes)
    try:
        yield conexion
    finally:
        cerrar_conexion(conexion)


def ejecutar_lectura(
    conexion: Any, comando: str, timeout: float = TIMEOUT_LECTURA
) -> str:
    """Ejecuta un comando del catálogo cerrado y devuelve la salida cruda.

    Rechaza cualquier comando que no esté en :data:`COMANDOS_LECTURA`; es la
    barrera que impide que una instrucción del modelo se convierta en un
    comando RouterOS arbitrario.
    """
    if comando not in COMANDOS_LECTURA:
        raise ErrorHerramienta(
            "comando_no_autorizado",
            f"Comando de lectura no autorizado: {comando!r}",
        )

    try:
        salida = conexion.send_command(
            comando,
            read_timeout=timeout,
            strip_prompt=True,
            strip_command=True,
        )
    except (ReadTimeout, NetmikoTimeoutException) as exc:
        raise ErrorHerramienta(
            "tiempo_agotado",
            f"El equipo no respondió en {timeout:.0f}s al comando {comando!r}",
        ) from exc
    except Exception as exc:
        raise ErrorHerramienta(
            "comando",
            f"Falló el comando {comando!r}: {_resumen_excepcion(exc)}",
        ) from exc

    if salida is None:
        raise ErrorHerramienta(
            "respuesta_vacia", f"El equipo no devolvió salida para {comando!r}"
        )
    return salida


def validar_nombre_interfaz(interfaz: str) -> str:
    """Valida y normaliza el nombre de una interfaz MikroTik.

    Solo se admiten letras, dígitos, punto, guion y guion bajo, con un máximo
    de 64 caracteres. Así el nombre no puede romper el comando ni el ``[find]``.
    """
    if not isinstance(interfaz, str):
        raise ErrorHerramienta("parametros_invalidos", "La interfaz debe ser texto")
    nombre = interfaz.strip()
    if not _PATRON_INTERFAZ.fullmatch(nombre):
        raise ErrorHerramienta(
            "parametros_invalidos", f"Nombre de interfaz inválido: {interfaz!r}"
        )
    return nombre


def validar_comentario(comentario: str) -> str:
    """Valida el texto del comentario antes de construir el comando.

    Se permite texto imprimible (incluidos acentos y espacios) de hasta 200
    caracteres. Se rechazan comillas dobles, barra invertida, ``$`` y
    caracteres de control, que podrían cerrar la cadena o inyectar sintaxis.
    No se recorta el texto: la verificación posterior compara el valor exacto.
    """
    if not isinstance(comentario, str):
        raise ErrorHerramienta("parametros_invalidos", "El comentario debe ser texto")
    if len(comentario) > MAX_COMENTARIO:
        raise ErrorHerramienta(
            "parametros_invalidos",
            f"El comentario supera el máximo de {MAX_COMENTARIO} caracteres",
        )
    if not _PATRON_COMENTARIO.fullmatch(comentario):
        raise ErrorHerramienta(
            "parametros_invalidos",
            "El comentario contiene caracteres no permitidos (comillas dobles, "
            "barra invertida, '$', saltos de línea o caracteres de control)",
        )
    return comentario


def construir_comando_comentario(interfaz: str, comentario: str) -> str:
    """Construye el único comando de escritura admitido por la herramienta.

    El resultado solo puede modificar la propiedad ``comment`` de la interfaz
    indicada; cualquier intento de inyectar otra sentencia se rechaza antes de
    llegar a la sesión SSH.
    """
    nombre = validar_nombre_interfaz(interfaz)
    texto = validar_comentario(comentario)
    return f'/interface set [find name="{nombre}"] comment="{texto}"'


def ejecutar_modificacion(
    conexion: Any, interfaz: str, comentario: str, timeout: float = TIMEOUT_LECTURA
) -> str:
    """Envía el cambio de comentario ya validado y devuelve la respuesta del equipo.

    Se usa ``send_command_timing`` y **no** ``send_command``: en RouterOS el eco
    se repinta y el prompt se reimprime, de modo que la parada por patrón de
    prompt de ``send_command`` puede devolver el control con el prompt previo a
    la ejecución y una salida vacía, ocultando lo que el equipo contestó. La
    lectura por tiempo espera a que el canal guarde silencio y conserva esa
    respuesta, que el llamador registra como diagnóstico.

    No recibe el comando como texto libre: lo construye :func:
    `construir_comando_comentario`. Cualquier falla se traduce a
    :class:`ErrorHerramienta`; el llamador debe releer la interfaz antes de
    afirmar que el cambio se aplicó, porque un ``[find]`` sin coincidencias no
    imprime nada y se parece a un éxito.
    """
    comando = construir_comando_comentario(interfaz, comentario)
    try:
        salida = conexion.send_command_timing(
            comando,
            last_read=ESPERA_SILENCIO,
            read_timeout=timeout,
            strip_prompt=True,
            strip_command=True,
        )
    except (ReadTimeout, NetmikoTimeoutException) as exc:
        raise ErrorHerramienta(
            "tiempo_agotado",
            f"El equipo no respondió en {timeout:.0f}s al modificar el comentario "
            f"de {interfaz!r}",
        ) from exc
    except Exception as exc:
        raise ErrorHerramienta(
            "modificacion_fallida",
            f"Falló la modificación de {interfaz!r} ({type(exc).__name__})",
        ) from exc

    texto = salida if isinstance(salida, str) else ""
    coincidencia = _ERROR_ROUTEROS.search(texto)
    if coincidencia is not None:
        detalle = texto[coincidencia.start() :].splitlines()[0].strip()[:200]
        raise ErrorHerramienta(
            "modificacion_fallida",
            f"RouterOS rechazó el cambio en {interfaz!r}: {detalle}",
            respuesta_equipo=texto,
        )
    return texto
