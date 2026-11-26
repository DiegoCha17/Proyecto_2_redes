#!/usr/bin/env python3
"""Catálogo cerrado de herramientas del agente NetAgent-IA (Etapa 3).

Fuente única de verdad de lo que el agente puede hacer: nombre, descripción,
argumentos admitidos, riesgo (``lectura``/``escritura``) y la función que lo
ejecuta. Los esquemas de *function calling* se generan desde esta misma
declaración, de modo que lo que se le ofrece al modelo y lo que el código
acepta no puedan divergir.

Principios:
  * Falla cerrado: un nombre o un argumento que no figure aquí se rechaza con
    :class:`herramientas.comun.ErrorHerramienta` antes de tocar nada.
  * No recibe comandos: las escrituras son únicamente el comentario de una
    interfaz autorizada por ``config/dispositivos.yml`` y la gestión CU-02 por
    el playbook Ansible cerrado; no se acepta texto RouterOS libre.
  * La aprobación humana **no** viaja en los argumentos: ``confirmacion`` solo
    se puede pasar como opción de invocación desde :mod:`agente.ejecutar`.
  * Los adaptadores solo traducen el vocabulario del catálogo al de las
    herramientas; la validación y la verificación siguen en su módulo original.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from config.ajustes import Ajustes, obtener_ajustes
from herramientas.actualizar_comentario import actualizar_comentario
from herramientas.comun import ErrorHerramienta
from herramientas.estado_interfaces import consultar
from agente.consulta_ansible import (
    consultar_bloqueo as _consultar_bloqueo,
    gestionar_bloqueo as _gestionar_bloqueo,
)

# --- Riesgo de una herramienta ----------------------------------------------
# Solo las de riesgo "escritura" exigen aprobación humana explícita.
RIESGO_LECTURA = "lectura"
RIESGO_ESCRITURA = "escritura"
RIESGOS = frozenset({RIESGO_LECTURA, RIESGO_ESCRITURA})

# --- Nombres estables del catálogo ------------------------------------------
CONSULTAR_INTERFACES = "consultar_interfaces"
CONSULTAR_INVENTARIO = "consultar_inventario"
CONSULTAR_BLOQUEO = "consultar_bloqueo"
GESTIONAR_BLOQUEO = "gestionar_bloqueo"
ACTUALIZAR_COMENTARIO = "actualizar_comentario"


# --- Adaptadores hacia las herramientas existentes --------------------------
# No reimplementan nada: solo fijan el nombre de los parámetros con los que el
# modelo y el planificador hablan de cada herramienta.


def _consultar_interfaces(equipo: str, ajustes: Ajustes | None = None) -> dict[str, Any]:
    """Consulta de solo lectura de las interfaces de un equipo autorizado."""
    return consultar(equipo, ajustes=ajustes)


def _consultar_inventario(ajustes: Ajustes | None = None) -> dict[str, Any]:
    """Devuelve el inventario lógico sin abrir conexiones a los equipos."""
    ajustes = ajustes or obtener_ajustes()
    dispositivos = []
    for clave, dispositivo in ajustes.dispositivos.items():
        host = getattr(dispositivo, "host", "")
        dispositivos.append(
            {
                "clave": str(clave),
                "nombre": dispositivo.nombre,
                "rol": dispositivo.rol,
                "ip": host.strip() if isinstance(host, str) and host.strip() else None,
            }
        )
    return {
        "ok": True,
        "dispositivos": dispositivos,
        "changed": False,
        "error": None,
    }


def _actualizar_comentario(
    equipo: str,
    interfaz: str,
    comentario: str,
    confirmacion: bool = False,
    ajustes: Ajustes | None = None,
) -> dict[str, Any]:
    """Cambio del comentario de una interfaz autorizada.

    ``confirmacion`` llega en ``False`` por defecto para que una invocación
    accidental no escriba; :mod:`agente.ejecutar` es el único que la pone en
    ``True``, y solo con una marca de aprobación válida.
    """
    return actualizar_comentario(
        equipo,
        interfaz,
        comentario,
        confirmacion=confirmacion,
        ajustes=ajustes,
    )


def _gestionar_bloqueo_catalogo(
    equipo: str,
    ip: str,
    accion: str,
    confirmacion: bool = False,
    ajustes: Ajustes | None = None,
) -> dict[str, Any]:
    """Adaptador de la escritura CU-02 con aprobación inyectada por ejecutar."""
    return _gestionar_bloqueo(
        equipo,
        ip,
        accion,
        confirmacion=confirmacion,
        ajustes=ajustes,
    )


@dataclass(frozen=True)
class DefinicionHerramienta:
    """Declaración inmutable de una herramienta del catálogo."""

    nombre: str
    descripcion: str
    riesgo: str
    parametros: Mapping[str, Mapping[str, Any]]
    obligatorios: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.riesgo not in RIESGOS:
            raise ValueError(
                f"Riesgo desconocido en la herramienta {self.nombre!r}: {self.riesgo!r}"
            )
        sobrantes = [clave for clave in self.obligatorios if clave not in self.parametros]
        if sobrantes:
            raise ValueError(
                f"La herramienta {self.nombre!r} declara obligatorios sin parámetro: "
                f"{', '.join(sobrantes)}"
            )
        object.__setattr__(
            self,
            "parametros",
            MappingProxyType(
                {clave: MappingProxyType(dict(datos)) for clave, datos in self.parametros.items()}
            ),
        )

    def esquema(self) -> dict[str, Any]:
        """Esquema de *function calling* derivado de la propia declaración."""
        return {
            "type": "function",
            "function": {
                "name": self.nombre,
                "description": self.descripcion,
                "parameters": {
                    "type": "object",
                    "properties": {
                        clave: dict(datos) for clave, datos in self.parametros.items()
                    },
                    "required": list(self.obligatorios),
                    "additionalProperties": False,
                },
            },
        }

    def validar_argumentos(self, argumentos: Mapping[str, Any] | None) -> dict[str, str]:
        """Comprueba forma y tipos de los argumentos recibidos.

        Solo admite las claves declaradas, exige las obligatorias y acepta
        únicamente texto: nada de números, listas ni objetos que luego habría
        que interpretar. No valida el contenido (equipo autorizado, interfaz
        permitida, comentario seguro): eso es de :mod:`agente.politicas`.
        """
        if argumentos is None:
            argumentos = {}
        if not isinstance(argumentos, Mapping):
            raise ErrorHerramienta(
                "argumentos_invalidos",
                f"Los argumentos de {self.nombre!r} deben ser un objeto JSON",
            )

        admitidos = ", ".join(self.parametros)
        desconocidos = sorted(str(clave) for clave in set(argumentos) - set(self.parametros))
        if desconocidos:
            raise ErrorHerramienta(
                "argumentos_invalidos",
                f"Argumentos no admitidos en {self.nombre!r}: "
                f"{', '.join(desconocidos)}. Admitidos: {admitidos}",
            )

        faltantes = [clave for clave in self.obligatorios if clave not in argumentos]
        if faltantes:
            raise ErrorHerramienta(
                "argumentos_invalidos",
                f"Faltan argumentos obligatorios en {self.nombre!r}: "
                f"{', '.join(faltantes)}",
            )

        limpios: dict[str, str] = {}
        for clave, valor in argumentos.items():
            if not isinstance(valor, str):
                raise ErrorHerramienta(
                    "argumentos_invalidos",
                    f"El argumento {clave!r} de {self.nombre!r} debe ser texto",
                )
            enumeracion = self.parametros.get(str(clave), {}).get("enum")
            if enumeracion is not None and valor not in enumeracion:
                valores = ", ".join(map(str, enumeracion))
                raise ErrorHerramienta(
                    "argumentos_invalidos",
                    f"El argumento {clave!r} de {self.nombre!r} debe ser uno de: {valores}",
                )
            limpios[str(clave)] = valor
        return limpios


@dataclass(frozen=True)
class Herramienta:
    """Herramienta del catálogo: su declaración y la función que la ejecuta."""

    definicion: DefinicionHerramienta
    funcion: Callable[..., dict[str, Any]]

    @property
    def nombre(self) -> str:
        return self.definicion.nombre

    @property
    def riesgo(self) -> str:
        return self.definicion.riesgo

    @property
    def descripcion(self) -> str:
        return self.definicion.descripcion

    def esquema(self) -> dict[str, Any]:
        return self.definicion.esquema()

    def validar_argumentos(self, argumentos: Mapping[str, Any] | None) -> dict[str, str]:
        return self.definicion.validar_argumentos(argumentos)

    def invocar(
        self, argumentos: Mapping[str, Any] | None, **opciones: Any
    ) -> dict[str, Any]:
        """Ejecuta la herramienta con argumentos ya validados en forma.

        Las ``opciones`` (``ajustes``, ``confirmacion``) no pueden venir de
        quien redactó los argumentos: las aporta :mod:`agente.ejecutar`.
        """
        limpios = self.validar_argumentos(argumentos)
        try:
            salida = self.funcion(**limpios, **opciones)
        except ErrorHerramienta:
            raise
        except Exception as exc:  # nunca devolver una traza al agente
            raise ErrorHerramienta(
                "interno",
                f"Error inesperado en la herramienta {self.nombre!r}: "
                f"{type(exc).__name__}",
            ) from exc

        if not isinstance(salida, dict):
            raise ErrorHerramienta(
                "interno",
                f"La herramienta {self.nombre!r} no devolvió una respuesta estructurada",
            )
        return salida


class Catalogo:
    """Conjunto cerrado de herramientas disponibles para el agente."""

    def __init__(self, herramientas: Iterable[Herramienta]) -> None:
        declaradas: dict[str, Herramienta] = {}
        for herramienta in herramientas:
            if herramienta.nombre in declaradas:
                raise ValueError(f"Herramienta duplicada en el catálogo: {herramienta.nombre!r}")
            declaradas[herramienta.nombre] = herramienta
        if not declaradas:
            raise ValueError("El catálogo no puede estar vacío")
        self._herramientas: Mapping[str, Herramienta] = MappingProxyType(declaradas)

    def nombres(self) -> tuple[str, ...]:
        """Nombres autorizados, en el orden en que se declararon."""
        return tuple(self._herramientas)

    def obtener(self, nombre: str) -> Herramienta:
        """Devuelve la herramienta pedida o falla cerrado."""
        try:
            return self._herramientas[str(nombre)]
        except KeyError:
            raise ErrorHerramienta(
                "herramienta_no_autorizada",
                f"Herramienta no autorizada: {nombre!r}. "
                f"Disponibles: {', '.join(self.nombres())}",
            ) from None

    def esquemas(self) -> list[dict[str, Any]]:
        """Esquemas de *function calling* de todo el catálogo."""
        return [herramienta.esquema() for herramienta in self._herramientas.values()]

    def invocar(
        self, nombre: str, argumentos: Mapping[str, Any] | None, **opciones: Any
    ) -> dict[str, Any]:
        """Valida el nombre y delega en la herramienta correspondiente."""
        return self.obtener(nombre).invocar(argumentos, **opciones)


@lru_cache(maxsize=1)
def catalogo_por_defecto() -> Catalogo:
    """Catálogo real del proyecto (lecturas y escritura autorizada).

    Se construye bajo demanda y se reutiliza; las herramientas reciben
    ``ajustes`` por inyección en cada invocación, nunca al declararse.
    """
    return Catalogo(
        [
            Herramienta(
                definicion=DefinicionHerramienta(
                    nombre=CONSULTAR_BLOQUEO,
                    descripcion="Consulta en R1 la regla administrada de una IPv4 LAN hacia WAN; por defecto PC1. No demuestra el bloqueo efectivo y no modifica el firewall.",
                    riesgo=RIESGO_LECTURA,
                    parametros={"equipo": {"type": "string", "description": "Únicamente r1"},
                                "ip": {"type": "string", "format": "ipv4", "description": "IPv4 LAN autorizada; opcional, por defecto PC1"}},
                    obligatorios=("equipo",),
                ),
                funcion=_consultar_bloqueo,
            ),
            Herramienta(
                definicion=DefinicionHerramienta(
                    nombre=CONSULTAR_INVENTARIO,
                    descripcion=(
                        "Lista el inventario lógico configurado en "
                        "config/dispositivos.yml: clave, nombre, rol y dirección IP "
                        "de gestión, sin consultar ni modificar los equipos."
                    ),
                    riesgo=RIESGO_LECTURA,
                    parametros={},
                    obligatorios=(),
                ),
                funcion=_consultar_inventario,
            ),
            Herramienta(
                definicion=DefinicionHerramienta(
                    nombre=CONSULTAR_INTERFACES,
                    descripcion=(
                        "Consulta en modo solo lectura el estado de las interfaces de "
                        "un equipo autorizado: nombre, estado, dirección IP y "
                        "comentario. Los estados devueltos son running, down o "
                        "disabled; el agente puede filtrar esos resultados sin "
                        "modificar la configuración."
                    ),
                    riesgo=RIESGO_LECTURA,
                    parametros={
                        "equipo": {
                            "type": "string",
                            "description": (
                                "Clave del equipo autorizado en "
                                "config/dispositivos.yml: r1 o sw1"
                            ),
                        }
                    },
                    obligatorios=("equipo",),
                ),
                funcion=_consultar_interfaces,
            ),
            Herramienta(
                definicion=DefinicionHerramienta(
                    nombre=GESTIONAR_BLOQUEO,
                    descripcion=(
                        "Bloquea o permite una IPv4 LAN en R1 mediante la regla "
                        "administrada de NetAgent-IA. Es una escritura idempotente, "
                        "requiere aprobación humana y verifica el estado posterior."
                    ),
                    riesgo=RIESGO_ESCRITURA,
                    parametros={
                        "equipo": {
                            "type": "string",
                            "enum": ["r1"],
                            "description": "Clave del equipo R1",
                        },
                        "ip": {
                            "type": "string",
                            "format": "ipv4",
                            "description": "IPv4 de la LAN autorizada por la política",
                        },
                        "accion": {
                            "type": "string",
                            "enum": ["bloquear", "permitir"],
                            "description": "Operación solicitada",
                        },
                    },
                    obligatorios=("equipo", "ip", "accion"),
                ),
                funcion=_gestionar_bloqueo_catalogo,
            ),
            Herramienta(
                definicion=DefinicionHerramienta(
                    nombre=ACTUALIZAR_COMENTARIO,
                    descripcion=(
                        "Cambia únicamente el comentario de una interfaz incluida en "
                        "el catálogo de interfaces autorizadas del equipo. Requiere "
                        "aprobación humana explícita y verifica el resultado releyendo "
                        "la interfaz. No acepta comandos RouterOS."
                    ),
                    riesgo=RIESGO_ESCRITURA,
                    parametros={
                        "equipo": {
                            "type": "string",
                            "description": (
                                "Clave del equipo autorizado en "
                                "config/dispositivos.yml: r1 (sw1 no admite escrituras)"
                            ),
                        },
                        "interfaz": {
                            "type": "string",
                            "description": (
                                "Interfaz autorizada del equipo; en el laboratorio solo "
                                "ether2 y ether4 de R1"
                            ),
                        },
                        "comentario": {
                            "type": "string",
                            "description": (
                                "Texto nuevo del comentario (máximo 200 caracteres, sin "
                                "comillas dobles, barra invertida ni '$')"
                            ),
                        },
                    },
                    obligatorios=("equipo", "interfaz", "comentario"),
                ),
                funcion=_actualizar_comentario,
            ),
        ]
    )
