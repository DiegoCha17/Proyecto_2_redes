#!/usr/bin/env python3
"""Terminal conversacional de NetAgent-IA.

Inicia con:

    python main.py

El programa carga la configuración central (``config/ajustes.py`` + ``.env``),
comprueba que exista la clave del proveedor de IA y abre una conversación en
español. Cada modificación se muestra como vista previa y solo se ejecuta si la
persona responde que sí en la terminal; ``/salir`` cierra la sesión.

Este archivo solo se ocupa de la entrada y la salida: las decisiones de
seguridad las aplican ``agente.politicas``, ``agente.ejecutar`` y la propia
herramienta. Aquí no se construye ningún comando RouterOS ni se abre ninguna
conexión directamente.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from agente.cerebro import Cerebro  # noqa: E402
from config.ajustes import Ajustes, ErrorConfiguracion, obtener_ajustes  # noqa: E402

COMANDO_SALIDA = "/salir"
PROMPT = "NetAgent> "

_EJEMPLOS = (
    '  "Analiza R1"',
    '  "Muéstrame las interfaces de SW1"',
    '  "¿Qué interfaces están desconectadas?"',
    '  "Cambia el comentario de ether4 en R1"',
)


def banner(ajustes: Ajustes) -> str:
    """Texto de bienvenida, sin credenciales ni direcciones de gestión."""
    lineas = [
        "",
        "NetAgent-IA — asistente de configuración del laboratorio MikroTik",
        f"Modelo: {ajustes.modelo}    Pasos por sesión: {ajustes.max_pasos}",
        "",
        "Equipos autorizados:",
    ]
    for dispositivo in ajustes.dispositivos.values():
        escritura = (
            ", ".join(dispositivo.interfaces_autorizadas)
            if dispositivo.interfaces_autorizadas
            else "sin escrituras"
        )
        lineas.append(
            f"  - {dispositivo.nombre} ({dispositivo.rol}); escritura permitida en: {escritura}"
        )
    lineas.extend(
        [
            "",
            "Ejemplos:",
            *_EJEMPLOS,
            "",
            "Toda modificación se muestra antes de aplicarse y requiere tu aprobación.",
            f"Escribe {COMANDO_SALIDA} para terminar.",
            "",
        ]
    )
    return "\n".join(lineas)


def _bucle(cerebro: Cerebro) -> int:
    """Lee mensajes hasta que la persona salga o interrumpa."""
    while True:
        try:
            linea = input(PROMPT)
        except EOFError:
            print()
            print("Hasta luego.")
            return 0
        except KeyboardInterrupt:
            print()
            print("Sesión interrumpida por el usuario.")
            return 0

        texto = linea.strip()
        if not texto:
            continue
        if texto.lower() == COMANDO_SALIDA:
            print("Hasta luego.")
            return 0

        try:
            respuesta = cerebro.procesar(texto)
        except KeyboardInterrupt:
            print()
            print("Sesión interrumpida por el usuario.")
            return 0
        except Exception:
            # Sin trazas ni detalles técnicos: el registro tiene el detalle.
            print(
                "Error inesperado en el agente. No se puede confirmar el estado de "
                "la operación; revisa el registro de la sesión antes de repetirla."
            )
            continue
        print(respuesta.texto)


def main(cerebro: Cerebro | None = None) -> int:
    """Punto de entrada de la terminal conversacional."""
    if cerebro is None:
        try:
            ajustes = obtener_ajustes()
        except ErrorConfiguracion as exc:
            print(f"No se pudo cargar la configuración: {exc}")
            return 1

        faltantes = ajustes.variables_entorno_faltantes()
        if "OPENAI_API_KEY" in faltantes:
            print(
                "Falta OPENAI_API_KEY. Copie .env.example a .env y defina la clave "
                "del proveedor de IA."
            )
            return 1
        restantes = [nombre for nombre in faltantes if nombre != "OPENAI_API_KEY"]
        if restantes:
            print(
                "Aviso: faltan credenciales SSH ("
                + ", ".join(restantes)
                + "); las consultas a esos equipos fallarán hasta completarlas."
            )

        print(banner(ajustes))
        cerebro = Cerebro(ajustes)

    codigo = 0
    try:
        codigo = _bucle(cerebro)
    except KeyboardInterrupt:
        print()
        print("Sesión interrumpida por el usuario.")
        codigo = 0

    ruta = cerebro.ruta_registro
    if ruta is not None:
        print(f"Registro de la sesión: {ruta}")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
