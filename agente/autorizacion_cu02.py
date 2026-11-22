"""Contratos locales de validación y aprobación para CU-02.

La ejecución Ansible permanece en el adaptador del catálogo. El controlador
confiable invoca la confirmación del operador y conserva la marca fuera del
alcance del modelo.
"""

from dataclasses import dataclass
import ipaddress
from typing import Callable, Mapping

HERRAMIENTA = "gestionar_bloqueo"
PARAMETROS_FIJOS = {"equipo": "r1", "origen": "192.168.10.50", "destino": "172.16.100.10"}
ACCIONES_GESTION = frozenset({"bloquear", "permitir"})


@dataclass(frozen=True)
class PropuestaBloqueo:
    herramienta: str
    operacion: str
    equipo: str
    origen: str
    destino: str


@dataclass(frozen=True)
class AprobacionBloqueo:
    propuesta: PropuestaBloqueo

    def cubre(self, propuesta: PropuestaBloqueo) -> bool:
        return isinstance(propuesta, PropuestaBloqueo) and self.propuesta == propuesta


@dataclass(frozen=True)
class PropuestaGestionBloqueo:
    """Propuesta canónica de la herramienta registrada en el catálogo."""

    herramienta: str
    equipo: str
    ip: str
    accion: str


def preparar_propuesta_gestionar(argumentos: Mapping[str, str]) -> PropuestaGestionBloqueo:
    """Valida la forma de CU-02 antes de aplicar la política de red."""
    requeridos = {"equipo", "ip", "accion"}
    if not isinstance(argumentos, Mapping) or set(argumentos) != requeridos:
        raise ValueError("Argumentos de gestionar_bloqueo inválidos")
    equipo = str(argumentos["equipo"]).strip().casefold()
    if equipo != "r1":
        raise ValueError("gestionar_bloqueo solo permite R1")
    accion = argumentos["accion"]
    if accion not in ACCIONES_GESTION:
        raise ValueError("La acción debe ser bloquear o permitir")
    try:
        ip = ipaddress.ip_address(str(argumentos["ip"]).strip())
    except ValueError as exc:
        raise ValueError("La IP de gestionar_bloqueo no es válida") from exc
    if not isinstance(ip, ipaddress.IPv4Address):
        raise ValueError("gestionar_bloqueo solo admite IPv4")
    return PropuestaGestionBloqueo(HERRAMIENTA, "r1", str(ip), accion)


def preparar_propuesta(argumentos: Mapping[str, str]) -> PropuestaBloqueo:
    if not isinstance(argumentos, Mapping) or set(argumentos) != {"operacion", *PARAMETROS_FIJOS}:
        raise ValueError("Argumentos CU-02 inválidos")
    if argumentos["operacion"] not in ("bloquear", "desbloquear"):
        raise ValueError("Operación no autorizada")
    if any(argumentos[k] != v for k, v in PARAMETROS_FIJOS.items()):
        raise ValueError("Parámetros CU-02 no autorizados")
    return PropuestaBloqueo(HERRAMIENTA, argumentos["operacion"],
                            argumentos["equipo"], argumentos["origen"], argumentos["destino"])


def solicitar_aprobacion(propuesta: PropuestaBloqueo,
                         confirmar: Callable[[PropuestaBloqueo], bool]) -> AprobacionBloqueo | None:
    # El callback pertenece exclusivamente al controlador, no a la respuesta LLM.
    if not isinstance(propuesta, PropuestaBloqueo):
        raise TypeError("Se requiere una propuesta validada")
    validada = preparar_propuesta({"operacion": propuesta.operacion, "equipo": propuesta.equipo,
                                  "origen": propuesta.origen, "destino": propuesta.destino})
    if propuesta != validada:
        raise ValueError("Herramienta no autorizada")
    return AprobacionBloqueo(propuesta) if confirmar(propuesta) is True else None


def autorizada(propuesta: PropuestaBloqueo, aprobacion: AprobacionBloqueo | None) -> bool:
    return isinstance(aprobacion, AprobacionBloqueo) and aprobacion.cubre(propuesta)
