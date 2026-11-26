#!/usr/bin/env python3
"""Planificación determinista de las acciones del agente (Etapa 3).

Convierte una herramienta del catálogo y sus argumentos en un :class:`Plan`
revisable: los pasos que se seguirán, si hace falta una persona y una vista
previa lista para mostrar. No abre conexiones, no escribe y no consulta a
ningún modelo de IA; lo único que decide qué hacer es el llamador (hoy,
pruebas; en la Etapa 4, :mod:`agente.cerebro`).

La planificación no abre conexiones. El controlador lee el estado antes de
mostrar la propuesta y solicitar aprobación para una modificación.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from agente.catalogo import Catalogo, GESTIONAR_BLOQUEO
from agente.ejecutar import Resultado
from agente.politicas import Accion, preparar
from config.ajustes import Ajustes
from herramientas.comun import error_json

# Pasos que se muestran al operador. Son texto informativo: las garantías las
# aplican las capas, no estas cadenas.
_PASOS_LECTURA = (
    "1. Comprobar que el equipo figura en config/dispositivos.yml.",
    "2. Leer las interfaces con la herramienta de solo lectura.",
    "3. Devolver la información estructurada al operador.",
)

_PASOS_ESCRITURA = (
    "1. Comprobar equipo, interfaz y comentario contra config/dispositivos.yml.",
    "2. Leer el comentario actual con la herramienta de consulta.",
    "3. Mostrar la propuesta y pedir aprobación humana explícita (s/N).",
    "4. Enviar solo el cambio de comentario, con confirmacion=True.",
    "5. Releer la interfaz; el cambio se informa como aplicado solo si coincide.",
)

_PASOS_GESTION_BLOQUEO = (
    "1. Comprobar R1, la IPv4 y la política LAN autorizada.",
    "2. Consultar la regla administrada con la herramienta de lectura.",
    "3. Mostrar la propuesta y pedir aprobación humana explícita (s/N).",
    "4. Ejecutar la operación idempotente mediante Ansible.",
    "5. Consultar de nuevo la regla con la herramienta de lectura.",
)


@dataclass(frozen=True)
class Plan:
    """Acción revisable antes de ejecutarse."""

    accion: Accion
    pasos: tuple[str, ...]
    requiere_autorizacion: bool
    vista_previa: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "vista_previa", MappingProxyType(dict(self.vista_previa)))

    @property
    def herramienta(self) -> str:
        return self.accion.herramienta

    @property
    def riesgo(self) -> str:
        return self.accion.riesgo

    @property
    def es_escritura(self) -> bool:
        return self.accion.es_escritura

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable del plan (sin secretos ni salidas crudas)."""
        return {
            "accion": self.accion.a_dict(),
            "pasos": list(self.pasos),
            "requiere_autorizacion": self.requiere_autorizacion,
            "vista_previa": dict(self.vista_previa),
        }


def _vista_previa(accion: Accion) -> dict[str, Any]:
    """Resumen legible de lo que hará la acción, sin abrir conexión."""
    vista: dict[str, Any] = {
        "herramienta": accion.herramienta,
        "equipo": accion.equipo,
        "clave": accion.clave,
        "riesgo": accion.riesgo,
        "descripcion": accion.descripcion,
        "argumentos": dict(accion.argumentos),
        "requiere_aprobacion": accion.requiere_aprobacion,
    }

    if accion.herramienta == GESTIONAR_BLOQUEO:
        vista.update(
            {
                "efecto": (
                    "Solo se modificará la regla administrada NETAGENT_LAB_ para "
                    "la IP indicada hacia 172.16.100.10 en R1."
                ),
                "nota": "La operación no se ejecuta sin aprobación humana explícita.",
            }
        )
    elif accion.es_escritura:
        vista.update(
            {
                "efecto": (
                    f"Solo se modificará la propiedad comment de {accion.interfaz} "
                    f"en {accion.equipo}; ninguna otra configuración se toca."
                ),
                "comentario_anterior": None,
                "nota": (
                    "El comentario anterior se conoce al ejecutar: la vista previa "
                    "no abre conexión con el equipo."
                ),
            }
        )
    else:
        vista["efecto"] = (
            "Ninguno: la consulta usa solo comandos de lectura y no modifica la "
            "configuración."
        )
    return vista


def planificar(
    ajustes: Ajustes,
    herramienta: str,
    argumentos: Mapping[str, Any] | None = None,
    *,
    catalogo: Catalogo | None = None,
) -> Plan:
    """Valida la petición y devuelve el plan correspondiente.

    Lanza :class:`herramientas.comun.ErrorHerramienta` si la herramienta no
    existe o si el equipo, la interfaz o el comentario no están autorizados.
    """
    accion = preparar(ajustes, herramienta, argumentos, catalogo=catalogo)
    return Plan(
        accion=accion,
        pasos=(
            _PASOS_GESTION_BLOQUEO
            if accion.herramienta == GESTIONAR_BLOQUEO
            else _PASOS_ESCRITURA
            if accion.es_escritura
            else _PASOS_LECTURA
        ),
        requiere_autorizacion=accion.requiere_aprobacion,
        vista_previa=_vista_previa(accion),
    )


def cancelar(plan: Plan, motivo: str = "La modificación fue cancelada por el operador") -> Resultado:
    """Anula un plan sin ejecutarlo y devuelve el resultado estructurado."""
    return Resultado(
        ok=False,
        herramienta=plan.herramienta,
        riesgo=plan.riesgo,
        ejecutada=False,
        aprobada=False,
        salida=None,
        error=error_json("cancelado", motivo),
    )
