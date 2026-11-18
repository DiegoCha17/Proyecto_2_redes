#!/usr/bin/env python3
"""Políticas deterministas de autorización del agente (Etapa 3).

Concentra todas las decisiones de seguridad que **no** dependen del modelo de
IA: qué herramienta existe, qué equipo está autorizado, qué interfaz puede
modificarse y qué texto de comentario se admite. La fuente de verdad sigue
siendo ``config/dispositivos.yml`` leído por :mod:`config.ajustes`; este módulo
no duplica esa configuración, solo la aplica.

Garantías:
  * No abre conexiones y no escribe nada: solo valida y describe la acción.
  * Falla cerrado: cualquier herramienta, equipo, interfaz o comentario fuera
    del catálogo se rechaza con :class:`herramientas.comun.ErrorHerramienta`
    antes de que exista una sesión SSH.
  * Toda herramienta de riesgo ``escritura`` exige aprobación humana explícita.
    ``NETAGENT_REQUIERE_APROBACION`` no puede desactivarla: el ajuste se lee
    como información, pero la política se aplica siempre.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from agente.catalogo import (
    RIESGO_ESCRITURA,
    RIESGO_LECTURA,
    Catalogo,
    catalogo_por_defecto,
)
from config.ajustes import Ajustes, Dispositivo, ErrorConfiguracion
from herramientas.comun import ErrorHerramienta, validar_comentario, validar_nombre_interfaz


@dataclass(frozen=True)
class Accion:
    """Acción ya validada contra la configuración (todavía no ejecutada)."""

    herramienta: str
    riesgo: str
    clave: str
    equipo: str
    descripcion: str
    requiere_aprobacion: bool
    argumentos: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "argumentos", MappingProxyType(dict(self.argumentos)))

    @property
    def es_escritura(self) -> bool:
        return self.riesgo == RIESGO_ESCRITURA

    @property
    def interfaz(self) -> str | None:
        return self.argumentos.get("interfaz")

    @property
    def comentario(self) -> str | None:
        return self.argumentos.get("comentario")

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable de la acción."""
        return {
            "herramienta": self.herramienta,
            "riesgo": self.riesgo,
            "clave": self.clave,
            "equipo": self.equipo,
            "descripcion": self.descripcion,
            "requiere_aprobacion": self.requiere_aprobacion,
            "argumentos": dict(self.argumentos),
        }


def requiere_aprobacion_humana(riesgo: str) -> bool:
    """Indica si una acción de ese riesgo necesita una persona.

    Deliberadamente **no** consulta ``ajustes.requiere_aprobacion``: poner
    ``NETAGENT_REQUIERE_APROBACION=false`` reduce ruido en la interfaz, no
    elimina la confirmación de las escrituras.
    """
    return riesgo == RIESGO_ESCRITURA


def resolver_equipo(ajustes: Ajustes, equipo: str) -> Dispositivo:
    """Devuelve el dispositivo autorizado de esa clave o falla cerrado."""
    if not isinstance(equipo, str) or not equipo.strip():
        raise ErrorHerramienta(
            "parametros_invalidos", "El parámetro 'equipo' es obligatorio"
        )
    try:
        return ajustes.dispositivo(equipo.strip())
    except ErrorConfiguracion as exc:
        raise ErrorHerramienta("dispositivo_no_autorizado", str(exc)) from exc


def resolver_interfaz(dispositivo: Dispositivo, interfaz: str) -> str:
    """Valida el nombre y comprueba que la interfaz esté en el catálogo cerrado."""
    nombre = validar_nombre_interfaz(interfaz)
    if not dispositivo.interfaz_autorizada(nombre):
        autorizadas = ", ".join(dispositivo.interfaces_autorizadas) or "(ninguna)"
        raise ErrorHerramienta(
            "interfaz_no_autorizada",
            f"La interfaz {nombre!r} no está autorizada en {dispositivo.nombre}. "
            f"Interfaces permitidas: {autorizadas}. Se configura en "
            "config/dispositivos.yml → interfaces_autorizadas",
        )
    return nombre


def preparar(
    ajustes: Ajustes,
    herramienta: str,
    argumentos: Mapping[str, Any] | None = None,
    *,
    catalogo: Catalogo | None = None,
) -> Accion:
    """Valida una herramienta y sus argumentos y devuelve la acción resultante.

    No ejecuta nada: sirve tanto para planificar como para rechazar de
    inmediato lo que el modelo proponga fuera de catálogo.
    """
    catalogo = catalogo or catalogo_por_defecto()
    definicion = catalogo.obtener(herramienta)
    limpios = definicion.validar_argumentos(argumentos)
    dispositivo = resolver_equipo(ajustes, limpios["equipo"])
    aprobacion = requiere_aprobacion_humana(definicion.riesgo)

    if definicion.riesgo == RIESGO_LECTURA:
        return Accion(
            herramienta=definicion.nombre,
            riesgo=definicion.riesgo,
            clave=dispositivo.clave,
            equipo=dispositivo.nombre,
            descripcion=(
                f"Consultar el estado de las interfaces de {dispositivo.nombre} "
                "(solo lectura)"
            ),
            requiere_aprobacion=aprobacion,
            argumentos={"equipo": dispositivo.clave},
        )

    interfaz = resolver_interfaz(dispositivo, limpios["interfaz"])
    comentario = limpios["comentario"]
    # Se reutiliza la validación de herramientas.comun: una sola definición de
    # qué texto es un comentario seguro.
    validar_comentario(comentario)

    return Accion(
        herramienta=definicion.nombre,
        riesgo=definicion.riesgo,
        clave=dispositivo.clave,
        equipo=dispositivo.nombre,
        descripcion=(
            f'Cambiar el comentario de {interfaz} en {dispositivo.nombre} a "{comentario}"'
        ),
        requiere_aprobacion=aprobacion,
        argumentos={
            "equipo": dispositivo.clave,
            "interfaz": interfaz,
            "comentario": comentario,
        },
    )
