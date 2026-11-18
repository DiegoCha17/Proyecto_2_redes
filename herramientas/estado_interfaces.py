#!/usr/bin/env python3
"""Consulta de solo lectura del estado de interfaces de R1 y SW1.

Herramienta del catálogo cerrado de NetAgent-IA. Lee dos comandos fijos de
RouterOS y devuelve un JSON estructurado con nombre, estado, dirección IP y
comentario de cada interfaz.

Uso:
    python3 herramientas/estado_interfaces.py r1
    python3 herramientas/estado_interfaces.py sw1

La salida es siempre un único objeto JSON. Código de salida:
    0  lectura correcta
    1  error controlado (conexión, autenticación, equipo no autorizado, …)

No modifica configuración: los comandos están en el catálogo de solo lectura de
:mod:`herramientas.comun` (``/interface print`` y ``/ip address print``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from config.ajustes import Ajustes, ErrorConfiguracion, obtener_ajustes  # noqa: E402
from herramientas.comun import (  # noqa: E402
    COMANDO_DIRECCIONES,
    COMANDO_INTERFACES,
    ErrorHerramienta,
    conexion_dispositivo,
    construir_respuesta,
    ejecutar_lectura,
    error_json,
    imprimir_json,
)

# Secuencias ANSI que RouterOS puede incluir aunque Netmiko las filtre.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Inicio de un registro de "print detail": número de índice al inicio de línea.
_INICIO_REGISTRO = re.compile(r"^\s*\d+\s")

# Propiedades key=value; los valores con espacios van entre comillas.
_PROPIEDAD = re.compile(r'([A-Za-z][\w-]*)=("(?:[^"\\]|\\.)*"|\S*)')

# Inicio de una propiedad, para distinguir el comentario de las propiedades.
_INICIO_PROPIEDAD = re.compile(r"[A-Za-z][\w-]*=")

# Marca con la que ``print detail`` presenta el comentario de una entrada.
_MARCA_COMENTARIO = ";;;"


def _limpiar(linea: str) -> str:
    return _ANSI.sub("", linea).rstrip()


def _partir_comentario(linea: str) -> tuple[str, str | None]:
    """Separa una línea en (texto, comentario ``;;;``), si lo lleva.

    En ``print detail`` RouterOS imprime el comentario de una entrada como
    ``;;; texto``, no como la propiedad ``comment=``. La marca solo se trata
    como comentario si aparece antes de la primera propiedad de la línea, para
    no partir un valor entrecomillado que contenga ``;;;``.
    """
    marca = linea.find(_MARCA_COMENTARIO)
    if marca < 0:
        return linea, None
    primera = _INICIO_PROPIEDAD.search(linea)
    if primera is not None and primera.start() < marca:
        return linea, None
    texto = linea[marca + len(_MARCA_COMENTARIO) :].strip()
    return linea[:marca], texto or None


def dividir_bloques(salida: str) -> list[dict[str, Any]]:
    """Separa la salida de ``print detail`` en registros lógicos.

    Cada registro comienza con su índice y agrupa las líneas siguientes
    (continuación de propiedades y comentario). El comentario ``;;;`` se separa
    del resto para que no se confunda con una propiedad ni se arrastre a la
    entrada vecina; si aparece antes del primer índice se asocia a la entrada
    siguiente. Así se soportan RouterOS 6 y 7 y el ajuste de ancho de línea que
    aplica Netmiko.

    Devuelve, por registro, ``lineas`` (propiedades, ya sin el comentario) y
    ``comentario`` (texto del ``;;;`` o ``None`` si la entrada no tiene).
    """
    bloques: list[dict[str, Any]] = []
    actual: dict[str, Any] | None = None
    pendiente: str | None = None

    for linea in salida.splitlines():
        limpia = _limpiar(linea)
        if not limpia.strip():
            continue
        if limpia.lstrip().startswith(("Flags:", "Columns:")):
            continue

        texto, comentario = _partir_comentario(limpia)
        if _INICIO_REGISTRO.match(limpia):
            if actual is not None:
                bloques.append(actual)
            actual = {
                "lineas": [texto.strip()],
                "comentario": comentario if comentario is not None else pendiente,
            }
            pendiente = None
            continue

        if actual is None:
            # Variante en la que el comentario se imprime antes de su registro.
            if comentario is not None:
                pendiente = comentario
            continue

        if texto.strip():
            actual["lineas"].append(texto.strip())
        if comentario is not None and actual["comentario"] is None:
            actual["comentario"] = comentario

    if actual is not None:
        bloques.append(actual)
    return bloques


def dividir_registros(salida: str) -> list[str]:
    """Igual que :func:`dividir_bloques` pero como texto plano por registro."""
    return [" ".join(bloque["lineas"]) for bloque in dividir_bloques(salida)]


def extraer_propiedades(registro: str) -> dict[str, str]:
    """Extrae las propiedades ``clave=valor`` de un registro.

    Se queda con la primera aparición de cada clave para que un texto de
    comentario no pueda sobrescribir una propiedad real.
    """
    propiedades: dict[str, str] = {}
    for clave, valor in _PROPIEDAD.findall(registro):
        if valor.startswith('"') and valor.endswith('"'):
            valor = valor[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        propiedades.setdefault(clave, valor)
    return propiedades


def detectar_banderas(registro: str) -> set[str]:
    """Devuelve las banderas de estado (p. ej. ``R``, ``X``) del registro."""
    sin_indice = re.sub(r"^\s*\d+\s*", "", registro)
    corte = re.search(r"\b[a-z][\w-]*=", sin_indice)
    prefijo = sin_indice[: corte.start()] if corte else sin_indice
    if ";;;" in prefijo:  # línea de comentario, no hay banderas reales
        return set()
    return set(re.findall(r"\b[A-Z]{1,4}\b", prefijo))


def estado_interfaz(propiedades: dict[str, str], banderas: set[str]) -> str:
    """Traduce banderas/propiedades de RouterOS a un estado legible.

    Valores posibles: ``running``, ``down`` o ``disabled``.
    """
    if propiedades.get("disabled") == "yes" or "X" in banderas:
        return "disabled"
    if propiedades.get("running") == "yes" or "R" in banderas:
        return "running"
    return "down"


def parsear_interfaces(salida: str) -> list[dict[str, Any]]:
    """Convierte la salida de ``/interface print detail`` en una lista.

    El comentario se toma de la línea ``;;;`` (forma real de ``print detail``) y,
    si la entrada no la lleva, de la propiedad ``comment=`` (RouterOS 6 y salidas
    resumidas).
    """
    interfaces: list[dict[str, Any]] = []
    for bloque in dividir_bloques(salida):
        registro = " ".join(bloque["lineas"])
        propiedades = extraer_propiedades(registro)
        nombre = propiedades.get("name", "").strip()
        if not nombre:
            continue
        comentario = bloque["comentario"]
        if comentario is None:
            comentario = propiedades.get("comment", "")
        interfaces.append(
            {
                "nombre": nombre,
                "estado": estado_interfaz(propiedades, detectar_banderas(registro)),
                "ip": "",
                "comentario": comentario,
            }
        )
    return interfaces


def parsear_direcciones(salida: str) -> dict[str, list[str]]:
    """Agrupa las direcciones IPv4 activas por nombre de interfaz.

    Se omiten las direcciones deshabilitadas. Si una interfaz tiene varias, se
    conserva el orden de RouterOS y se eliminan duplicados.
    """
    direcciones: dict[str, list[str]] = {}
    for registro in dividir_registros(salida):
        propiedades = extraer_propiedades(registro)
        interfaz = propiedades.get("interface", "").strip()
        direccion = propiedades.get("address", "").strip()
        if not interfaz or not direccion:
            continue
        if propiedades.get("disabled") == "yes":
            continue
        lista = direcciones.setdefault(interfaz, [])
        if direccion not in lista:
            lista.append(direccion)
    return direcciones


def combinar(
    interfaces: list[dict[str, Any]], direcciones: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Asigna a cada interfaz su dirección IP (varias separadas por coma)."""
    for interfaz in interfaces:
        interfaz["ip"] = ", ".join(direcciones.get(interfaz["nombre"], []))
    return interfaces


def consultar(clave: str, ajustes: Ajustes | None = None) -> dict[str, Any]:
    """Consulta las interfaces del equipo autorizado y devuelve el sobre JSON.

    Nunca lanza excepciones hacia el llamador: cualquier falla se informa con
    ``ok=false`` y un objeto ``error`` con tipo y mensaje.
    """
    try:
        ajustes = ajustes or obtener_ajustes()
    except ErrorConfiguracion as exc:
        return construir_respuesta(str(clave), error=error_json("configuracion", str(exc)))

    try:
        dispositivo = ajustes.dispositivo(clave)
    except ErrorConfiguracion as exc:
        return construir_respuesta(
            str(clave), error=error_json("dispositivo_no_autorizado", str(exc))
        )

    try:
        with conexion_dispositivo(dispositivo.clave, ajustes=ajustes) as sesion:
            salida_interfaces = ejecutar_lectura(sesion, COMANDO_INTERFACES)
            salida_direcciones = ejecutar_lectura(sesion, COMANDO_DIRECCIONES)
    except ErrorConfiguracion as exc:
        return construir_respuesta(dispositivo.nombre, error=error_json("configuracion", str(exc)))
    except ErrorHerramienta as exc:
        return construir_respuesta(dispositivo.nombre, error=exc.a_dict())
    except Exception as exc:  # nunca devolver una traza al agente
        return construir_respuesta(
            dispositivo.nombre,
            error=error_json(
                "interno",
                f"Error inesperado durante la consulta: {type(exc).__name__}",
            ),
        )

    interfaces = parsear_interfaces(salida_interfaces)
    if not interfaces:
        return construir_respuesta(
            dispositivo.nombre,
            error=error_json(
                "respuesta_no_reconocida",
                "La salida de '/interface print detail' no contiene interfaces "
                "reconocibles; no se informa un resultado inventado",
            ),
        )

    return construir_respuesta(
        dispositivo.nombre,
        combinar(interfaces, parsear_direcciones(salida_direcciones)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consulta (solo lectura) las interfaces de un MikroTik autorizado.",
        epilog="Equipos autorizados: r1 y sw1, según config/dispositivos.yml.",
    )
    parser.add_argument("equipo", help="clave del equipo: r1 o sw1")
    args = parser.parse_args(argv)

    resultado = consultar(args.equipo)
    imprimir_json(resultado)
    return 0 if resultado["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
