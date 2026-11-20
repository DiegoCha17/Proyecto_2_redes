"""Contrato local de aprobación para una futura integración CU-02.

No ejecuta Ansible. El controlador confiable debe invocar la confirmación del
operador y conservar esta marca fuera del alcance del modelo.
"""

from dataclasses import dataclass
from typing import Callable, Mapping

HERRAMIENTA = "gestionar_bloqueo"
PARAMETROS_FIJOS = {"equipo": "r1", "origen": "192.168.10.50", "destino": "172.16.100.10"}


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
