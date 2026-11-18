#!/usr/bin/env python3
"""Cambio controlado del comentario de una interfaz MikroTik.

Herramienta del catálogo cerrado de NetAgent-IA. Solo permite modificar la
propiedad ``comment`` de una interfaz incluida explícitamente en
``config/dispositivos.yml`` (``interfaces_autorizadas``). No toca direcciones
IP, estado, bridge, VLAN, firewall ni ninguna otra configuración.

Uso:
    python3 herramientas/actualizar_comentario.py r1 ether4 "P2-NetAgent-IA-Demo" --confirmacion

Sin ``--confirmacion`` no se abre conexión ni se cambia nada: la herramienta
nunca asume que el operador desea modificar el equipo por el solo hecho de
ejecutarla. La salida es un único objeto JSON; código 0 si el resultado es
correcto (incluido el caso sin cambios) y 1 si hubo error.

Flujo:
    1. Validar parámetros, equipo e interfaz contra la configuración central.
    2. Exigir confirmación explícita.
    3. Leer el comentario actual; si ya coincide, no enviar nada (idempotencia).
    4. Enviar ``/interface set [find name=…] comment="…"`` (comando construido
       internamente, nunca recibido como texto libre) con lectura por tiempo
       (``send_command_timing``), que no depende del prompt repintado de
       RouterOS y conserva la respuesta del equipo.
    5. Releer la interfaz y solo entonces informar ``changed=true``; la
       respuesta del equipo se informa en ``respuesta_equipo`` como diagnóstico.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from config.ajustes import Ajustes, ErrorConfiguracion, obtener_ajustes  # noqa: E402
from herramientas.comun import (  # noqa: E402
    COMANDO_INTERFACES,
    ErrorHerramienta,
    conexion_dispositivo,
    ejecutar_lectura,
    ejecutar_modificacion,
    error_json,
    imprimir_json,
    validar_comentario,
    validar_nombre_interfaz,
)
from herramientas.estado_interfaces import parsear_interfaces  # noqa: E402

# La respuesta del equipo se conserva solo como diagnóstico: se recorta para no
# volcar en el JSON todo el repintado de la consola de RouterOS.
MAX_RESPUESTA_EQUIPO = 300


def _respuesta(
    equipo: str,
    interfaz: str,
    comentario_anterior: str | None = None,
    comentario_solicitado: str = "",
    changed: bool = False,
    confirmado: bool = False,
    verificacion: Mapping[str, Any] | None = None,
    respuesta_equipo: str | None = None,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Sobre JSON propio de esta herramienta (estructura estable)."""
    return {
        "ok": error is None,
        "equipo": equipo,
        "interfaz": interfaz,
        "comentario_anterior": comentario_anterior,
        "comentario_solicitado": comentario_solicitado,
        "changed": bool(changed),
        "confirmado": bool(confirmado),
        "verificacion": dict(verificacion) if verificacion is not None else None,
        "respuesta_equipo": respuesta_equipo,
        "error": dict(error) if error is not None else None,
    }


def _recortar_respuesta(salida: str | None) -> str | None:
    """Normaliza la respuesta del equipo para el JSON (máx. 300 caracteres)."""
    if not salida:
        return None
    return salida.strip()[:MAX_RESPUESTA_EQUIPO]


def _leer_comentario(sesion: Any, equipo: str, interfaz: str) -> str:
    """Lee el comentario actual de una interfaz o falla de forma controlada."""
    salida = ejecutar_lectura(sesion, COMANDO_INTERFACES)
    interfaces = parsear_interfaces(salida)
    if not interfaces:
        raise ErrorHerramienta(
            "respuesta_no_reconocida",
            "La salida de '/interface print detail' no contiene interfaces "
            "reconocibles; no se informa un resultado inventado",
        )
    for datos in interfaces:
        if datos["nombre"] == interfaz:
            return datos["comentario"]
    raise ErrorHerramienta(
        "interfaz_inexistente",
        f"La interfaz {interfaz!r} no existe en {equipo}",
    )


def actualizar_comentario(
    equipo: str,
    interfaz: str,
    comentario: str,
    confirmacion: bool = False,
    ajustes: Ajustes | None = None,
) -> dict[str, Any]:
    """Cambia (o no) el comentario de una interfaz y devuelve el sobre JSON.

    Nunca lanza excepciones hacia el llamador. ``changed`` solo es verdadero
    cuando la relectura posterior confirma el comentario solicitado.
    """
    equipo_texto = "" if equipo is None else str(equipo).strip()
    interfaz_texto = "" if interfaz is None else str(interfaz).strip()
    solicitado = comentario if isinstance(comentario, str) else ""

    def base(**extra: Any) -> dict[str, Any]:
        return _respuesta(
            equipo_texto,
            interfaz_texto,
            comentario_solicitado=solicitado,
            confirmado=confirmacion,
            **extra,
        )

    if not equipo_texto:
        return base(
            error=error_json(
                "parametros_invalidos", "El parámetro 'equipo' es obligatorio"
            )
        )

    try:
        ajustes = ajustes or obtener_ajustes()
    except ErrorConfiguracion as exc:
        return base(error=error_json("configuracion", str(exc)))

    try:
        dispositivo = ajustes.dispositivo(equipo_texto)
    except ErrorConfiguracion as exc:
        return base(error=error_json("dispositivo_no_autorizado", str(exc)))

    equipo_texto = dispositivo.nombre

    try:
        interfaz_texto = validar_nombre_interfaz(interfaz_texto)
        validar_comentario(comentario)
    except ErrorHerramienta as exc:
        return base(error=exc.a_dict())

    if not dispositivo.interfaz_autorizada(interfaz_texto):
        autorizadas = ", ".join(dispositivo.interfaces_autorizadas) or "(ninguna)"
        return base(
            error=error_json(
                "interfaz_no_autorizada",
                f"La interfaz {interfaz_texto!r} no está autorizada en "
                f"{dispositivo.nombre}. Interfaces permitidas: {autorizadas}. "
                "Se configura en config/dispositivos.yml → interfaces_autorizadas",
            )
        )

    if not confirmacion:
        return base(
            error=error_json(
                "confirmacion_requerida",
                "Se requiere confirmación explícita (--confirmacion) para "
                "modificar el comentario; no se abrió conexión ni se cambió nada",
            )
        )

    try:
        with conexion_dispositivo(dispositivo.clave, ajustes=ajustes) as sesion:
            try:
                anterior = _leer_comentario(sesion, equipo_texto, interfaz_texto)
            except ErrorHerramienta as exc:
                return base(error=exc.a_dict())

            if anterior == solicitado:
                return base(
                    comentario_anterior=anterior,
                    verificacion={
                        "ok": True,
                        "comentario_leido": anterior,
                        "coincide": True,
                    },
                )

            try:
                respuesta = ejecutar_modificacion(sesion, interfaz_texto, solicitado)
            except ErrorHerramienta as exc:
                return base(
                    comentario_anterior=anterior,
                    respuesta_equipo=_recortar_respuesta(exc.respuesta_equipo),
                    error=exc.a_dict(),
                )

            try:
                posterior = _leer_comentario(sesion, equipo_texto, interfaz_texto)
            except ErrorHerramienta as exc:
                return base(
                    comentario_anterior=anterior,
                    verificacion={
                        "ok": False,
                        "comentario_leido": None,
                        "coincide": False,
                    },
                    respuesta_equipo=_recortar_respuesta(respuesta),
                    error=error_json(
                        "verificacion_fallida",
                        "El cambio se envió pero no se pudo volver a leer la "
                        f"interfaz; no se afirma que se haya aplicado: {exc.mensaje}",
                    ),
                )

            if posterior != solicitado:
                return base(
                    comentario_anterior=anterior,
                    verificacion={
                        "ok": False,
                        "comentario_leido": posterior,
                        "coincide": False,
                    },
                    respuesta_equipo=_recortar_respuesta(respuesta),
                    error=error_json(
                        "verificacion_fallida",
                        "La relectura no muestra el comentario solicitado; el "
                        "cambio no se considera aplicado",
                    ),
                )

            return base(
                comentario_anterior=anterior,
                changed=True,
                verificacion={
                    "ok": True,
                    "comentario_leido": posterior,
                    "coincide": True,
                },
                respuesta_equipo=_recortar_respuesta(respuesta),
            )
    except ErrorConfiguracion as exc:
        return base(error=error_json("configuracion", str(exc)))
    except ErrorHerramienta as exc:
        return base(
            respuesta_equipo=_recortar_respuesta(exc.respuesta_equipo),
            error=exc.a_dict(),
        )
    except Exception as exc:  # nunca devolver una traza al agente
        return base(
            error=error_json(
                "interno",
                f"Error inesperado durante la actualización: {type(exc).__name__}",
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cambia únicamente el comentario de una interfaz MikroTik "
            "autorizada. Sin --confirmacion no se modifica nada."
        ),
        epilog=(
            "Ejemplo: python3 herramientas/actualizar_comentario.py "
            'r1 ether4 "P2-NetAgent-IA-Demo" --confirmacion'
        ),
    )
    parser.add_argument("equipo", help="clave del equipo: r1 o sw1")
    parser.add_argument("interfaz", help="interfaz autorizada en config/dispositivos.yml")
    parser.add_argument("comentario", help="texto del nuevo comentario (máx. 200)")
    parser.add_argument(
        "--confirmacion",
        action="store_true",
        help="confirma explícitamente la modificación; sin esta opción no se ejecuta",
    )
    args = parser.parse_args(argv)

    resultado = actualizar_comentario(
        args.equipo,
        args.interfaz,
        args.comentario,
        confirmacion=args.confirmacion,
    )
    imprimir_json(resultado)
    return 0 if resultado["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
