#!/usr/bin/env python3
"""Ejecución de acciones del agente con triple barrera de autorización (Etapa 3).

Es el único punto del agente que invoca herramientas reales. Antes de escribir
exige una :class:`Aprobacion` explícita —una marca que solo crea la interfaz de
operador cuando la persona responde que sí— y pasa ``confirmacion=True`` a la
herramienta de escritura, que a su vez vuelve a validar equipo, interfaz y
comentario y verifica el resultado releyendo el equipo.

Barreras para una modificación:
  1. ``Aprobacion`` presente y que **cubra esta acción concreta** (herramienta,
     equipo, interfaz y texto); una aprobación de otro cambio no sirve.
  2. ``confirmacion=True`` en la llamada a :mod:`herramientas.actualizar_comentario`.
  3. Validación y verificación propias de la herramienta original (intactas).

Las lecturas no requieren aprobación: no cambian configuración. Ninguna acción
admite comandos RouterOS ni texto libre que llegue a la sesión SSH: la
herramienta solo recibe los argumentos del catálogo.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from agente.catalogo import Catalogo, catalogo_por_defecto
from agente.politicas import Accion, requiere_aprobacion_humana
from config.ajustes import Ajustes
from herramientas.comun import ErrorHerramienta, error_json


@dataclass(frozen=True)
class Aprobacion:
    """Marca de aprobación humana para una acción concreta.

    Solo la crea :func:`aprobar` después de que la persona confirme. No cubre
    otra herramienta, otro equipo, otra interfaz ni otro comentario: si el
    texto cambia, hay que volver a preguntar.
    """

    herramienta: str
    clave: str
    interfaz: str | None = None
    comentario: str | None = None
    otorgada_por: str = "operador"

    def cubre(self, accion: Accion) -> bool:
        """Indica si esta aprobación autoriza exactamente esa acción."""
        return (
            self.herramienta == accion.herramienta
            and self.clave == accion.clave
            and self.interfaz == accion.interfaz
            and self.comentario == accion.comentario
        )

    def a_dict(self) -> dict[str, Any]:
        return {
            "herramienta": self.herramienta,
            "clave": self.clave,
            "interfaz": self.interfaz,
            "comentario": self.comentario,
            "otorgada_por": self.otorgada_por,
        }


@dataclass(frozen=True)
class Resultado:
    """Resultado estructurado de una ejecución.

    ``ejecutada`` indica que la herramienta llegó a invocarse; ``aprobada``,
    que se presentó una marca de aprobación válida. Una acción de escritura sin
    aprobación nunca se ejecuta, y una lectura nunca figura como aprobada.
    """

    ok: bool
    herramienta: str
    riesgo: str
    ejecutada: bool
    aprobada: bool
    salida: Mapping[str, Any] | None = None
    error: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.salida is not None:
            object.__setattr__(self, "salida", MappingProxyType(dict(self.salida)))
        if self.error is not None:
            object.__setattr__(self, "error", MappingProxyType(dict(self.error)))

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable del resultado."""
        return {
            "ok": self.ok,
            "herramienta": self.herramienta,
            "riesgo": self.riesgo,
            "ejecutada": self.ejecutada,
            "aprobada": self.aprobada,
            "salida": dict(self.salida) if self.salida is not None else None,
            "error": dict(self.error) if self.error is not None else None,
        }


def aprobar(accion: Accion, otorgada_por: str = "operador") -> Aprobacion:
    """Crea la marca de aprobación de una acción revisada.

    Debe llamarse **solo** tras recibir un sí explícito de la persona: es un
    acto del operador, no del modelo.
    """
    return Aprobacion(
        herramienta=accion.herramienta,
        clave=accion.clave,
        interfaz=accion.interfaz,
        comentario=accion.comentario,
        otorgada_por=otorgada_por,
    )


def _rechazo(accion: Accion, tipo: str, mensaje: str) -> Resultado:
    """Resultado de una acción que no se ejecutó."""
    return Resultado(
        ok=False,
        herramienta=accion.herramienta,
        riesgo=accion.riesgo,
        ejecutada=False,
        aprobada=False,
        salida=None,
        error=error_json(tipo, mensaje),
    )


def ejecutar(
    accion: Accion,
    aprobacion: Aprobacion | None = None,
    *,
    catalogo: Catalogo | None = None,
    ajustes: Ajustes | None = None,
) -> Resultado:
    """Ejecuta una acción validada y devuelve siempre un resultado estructurado.

    Para escrituras, sin ``aprobacion`` válida no se invoca ninguna herramienta:
    no hay conexión, ni siquiera de lectura. Nunca propaga excepciones hacia el
    agente; las fallas se traducen a ``error`` con tipo estable.
    """
    if not isinstance(accion, Accion):
        raise TypeError("ejecutar() requiere una Accion construida por agente.politicas")

    catalogo = catalogo or catalogo_por_defecto()

    if requiere_aprobacion_humana(accion.riesgo):
        if aprobacion is None:
            return _rechazo(
                accion,
                "aprobacion_requerida",
                "Toda modificación requiere aprobación humana explícita; no se "
                "abrió conexión ni se cambió nada",
            )
        if not isinstance(aprobacion, Aprobacion) or not aprobacion.cubre(accion):
            return _rechazo(
                accion,
                "aprobacion_invalida",
                "La aprobación no corresponde a esta modificación (herramienta, "
                "equipo, interfaz o comentario distintos); no se abrió conexión "
                "ni se cambió nada",
            )
        opciones: dict[str, Any] = {"confirmacion": True, "ajustes": ajustes}
        aprobada = True
    else:
        opciones = {"ajustes": ajustes}
        aprobada = False

    try:
        salida = catalogo.invocar(accion.herramienta, dict(accion.argumentos), **opciones)
    except ErrorHerramienta as exc:
        return _rechazo(accion, exc.tipo, exc.mensaje)
    except Exception as exc:  # nunca devolver una traza al agente
        return _rechazo(
            accion,
            "interno",
            f"Error inesperado al ejecutar {accion.herramienta!r}: {type(exc).__name__}",
        )

    error = salida.get("error")
    return Resultado(
        ok=bool(salida.get("ok")) and not error,
        herramienta=accion.herramienta,
        riesgo=accion.riesgo,
        ejecutada=True,
        aprobada=aprobada,
        salida=salida,
        error=error,
    )
