#!/usr/bin/env python3
"""Coordinación del agente conversacional NetAgent-IA (Etapa 3).

Encadena las capas ya probadas —interpretación, políticas, planificación y
ejecución— y añade lo que solo tiene sentido con una persona delante: mostrar la
vista previa, pedir aprobación, cancelar sin ejecutar y dejar un registro JSON
de la sesión.

Reglas que aplica este módulo:
  * La aprobación es un acto del operador. Solo el ``confirmar`` inyectado (la
    consola) puede originar una :class:`agente.ejecutar.Aprobacion`; ni el texto
    ni los argumentos que propone el modelo participan en esa decisión.
  * Toda modificación se muestra antes de ejecutarse y, sin un «sí» explícito,
    no se invoca ninguna herramienta (ni siquiera una lectura).
  * Se respeta ``NETAGENT_MAX_PASOS``: cada mensaje del operador consume un paso.
  * No hay reintentos: cada acción se ejecuta como máximo una vez.
  * Si la verificación falla, el resumen nunca afirma que el cambio se aplicó.
  * El registro no guarda credenciales, claves de API ni datos de entorno.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from agente.catalogo import Catalogo, catalogo_por_defecto
from agente.ejecutar import Resultado, aprobar, ejecutar
from agente.interpretar import (
    TIPO_HERRAMIENTA,
    TIPO_MENSAJE,
    ErrorProveedor,
    Intencion,
    ProveedorIA,
    interpretar,
)
from agente.planificador import Plan, cancelar, planificar
from config.ajustes import (
    DIRECTORIO_REGISTROS,
    Ajustes,
    obtener_ajustes,
)
from herramientas.comun import ErrorHerramienta, error_json

# --- Tipos de respuesta al operador -----------------------------------------
RESPUESTA_MENSAJE = "mensaje"
RESPUESTA_CONSULTA = "consulta"
RESPUESTA_MODIFICACION = "modificacion"
RESPUESTA_CANCELADO = "cancelado"
RESPUESTA_ERROR = "error"

# Longitud máxima de cualquier texto que se guarde en el registro.
_MAX_TEXTO_REGISTRO = 500

# Nombres de campo que jamás deben aparecer con valor en un registro.
_MARCAS_SENSIBLES = (
    "api_key",
    "apikey",
    "password",
    "contrasena",
    "contraseña",
    "token",
    "secret",
    "credencial",
)


def _es_campo_sensible(nombre: str) -> bool:
    """Indica si un campo del registro podría contener un secreto."""
    minusculas = nombre.lower()
    return any(marca in minusculas for marca in _MARCAS_SENSIBLES) or minusculas.endswith(
        "_clave"
    )


def _sanear(dato: Any) -> Any:
    """Copia un dato dejando el registro libre de secretos y textos gigantes."""
    if isinstance(dato, Mapping):
        return {
            str(clave): (
                "<oculto>" if _es_campo_sensible(str(clave)) else _sanear(valor)
            )
            for clave, valor in dato.items()
        }
    if isinstance(dato, (list, tuple)):
        return [_sanear(elemento) for elemento in dato]
    if isinstance(dato, str):
        return dato[:_MAX_TEXTO_REGISTRO]
    return dato


class RegistroSesion:
    """Registro JSON de una sesión, reescrito tras cada evento.

    Un fallo de escritura no interrumpe la conversación: se expone en
    ``error`` para que el agente avise una sola vez y siga trabajando.
    """

    def __init__(
        self,
        directorio: str | Path | None = None,
        modelo: str = "",
        momento: datetime | None = None,
    ) -> None:
        self._directorio = Path(directorio) if directorio is not None else DIRECTORIO_REGISTROS
        self._modelo = modelo
        self._inicio = momento or datetime.now()
        self._eventos: list[dict[str, Any]] = []
        self._ruta = self._directorio / f"sesion-{self._inicio:%Y%m%d-%H%M%S}.json"
        self.error: str | None = None

    @property
    def ruta(self) -> Path:
        return self._ruta

    @property
    def existe(self) -> bool:
        return self._ruta.is_file()

    @property
    def eventos(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._eventos)

    def anotar(self, evento: Mapping[str, Any]) -> None:
        """Añade un evento y reescribe el archivo de la sesión."""
        limpio = {
            "paso": len(self._eventos) + 1,
            "momento": datetime.now().isoformat(timespec="seconds"),
        }
        limpio.update(_sanear(dict(evento)))
        self._eventos.append(limpio)
        self._guardar()

    def _guardar(self) -> None:
        contenido = {
            "sesion": self._inicio.isoformat(timespec="seconds"),
            "modelo": self._modelo,
            "eventos": self._eventos,
        }
        try:
            self._directorio.mkdir(parents=True, exist_ok=True)
            self._ruta.write_text(
                json.dumps(contenido, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.error = None
        except OSError as exc:
            self.error = f"{type(exc).__name__}: no se pudo escribir el archivo"


@dataclass(frozen=True)
class Respuesta:
    """Respuesta de un turno del agente, lista para imprimirse o registrarse."""

    tipo: str
    texto: str
    ok: bool
    resultado: Mapping[str, Any] | None = None
    error: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.resultado is not None:
            object.__setattr__(self, "resultado", MappingProxyType(dict(self.resultado)))
        if self.error is not None:
            object.__setattr__(self, "error", MappingProxyType(dict(self.error)))

    def a_dict(self) -> dict[str, Any]:
        """Representación serializable de la respuesta."""
        return {
            "tipo": self.tipo,
            "texto": self.texto,
            "ok": self.ok,
            "resultado": dict(self.resultado) if self.resultado is not None else None,
            "error": dict(self.error) if self.error is not None else None,
        }


def _imprimir(texto: str) -> None:
    print(texto)


def confirmar_en_consola(plan: Plan) -> bool:
    """Pregunta al operador por la consola; cualquier otra cosa es «no»."""
    try:
        respuesta = input("¿Aplicar esta modificación? [s/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return respuesta.strip().lower() in {"s", "si", "sí", "y", "yes"}


class Cerebro:
    """Ciclo del agente: interpretar → planificar → aprobar → ejecutar → registrar."""

    def __init__(
        self,
        ajustes: Ajustes | None = None,
        *,
        proveedor: ProveedorIA | None = None,
        catalogo: Catalogo | None = None,
        salida: Callable[[str], None] | None = None,
        confirmar: Callable[[Plan], bool] | None = None,
        registro: RegistroSesion | None = None,
    ) -> None:
        self.ajustes = ajustes or obtener_ajustes()
        self._proveedor = proveedor
        self._catalogo = catalogo or catalogo_por_defecto()
        self._salida = salida or _imprimir
        self._confirmar = confirmar or confirmar_en_consola
        self._registro = (
            registro
            if registro is not None
            else RegistroSesion(modelo=self.ajustes.modelo)
        )
        self.pasos_usados = 0
        self._aviso_registro_mostrado = False

    # --- Estado visible ------------------------------------------------------

    @property
    def catalogo(self) -> Catalogo:
        return self._catalogo

    @property
    def ruta_registro(self) -> Path | None:
        if self._registro is None or not self._registro.existe:
            return None
        return self._registro.ruta

    @property
    def error_registro(self) -> str | None:
        return self._registro.error if self._registro is not None else None

    @property
    def pasos_restantes(self) -> int:
        return max(0, self.ajustes.max_pasos - self.pasos_usados)

    # --- Turno ---------------------------------------------------------------

    def procesar(self, mensaje: str) -> Respuesta:
        """Atiende un mensaje del operador y devuelve la respuesta del turno."""
        consulta = mensaje.strip() if isinstance(mensaje, str) else ""
        if not consulta:
            return Respuesta(
                tipo=RESPUESTA_ERROR,
                ok=False,
                texto="Escribe una consulta, o escribe /salir para terminar.",
                error=error_json(
                    "parametros_invalidos", "El mensaje del operador está vacío"
                ),
            )

        if self.pasos_usados >= self.ajustes.max_pasos:
            return self._respuesta_limite(consulta)

        self.pasos_usados += 1

        try:
            intencion = interpretar(
                consulta,
                proveedor=self._proveedor,
                catalogo=self._catalogo,
                ajustes=self.ajustes,
            )
        except (ErrorProveedor, ErrorHerramienta) as exc:
            return self._respuesta_error(
                consulta, exc.tipo, exc.mensaje, explicacion=exc.a_dict()
            )

        if intencion.tipo == TIPO_MENSAJE:
            texto = intencion.mensaje or "No obtuve una respuesta del modelo."
            self._anotar(consulta, intencion=intencion, nota="respuesta conversacional")
            return Respuesta(tipo=RESPUESTA_MENSAJE, texto=texto, ok=True)

        if intencion.tipo != TIPO_HERRAMIENTA:
            return self._respuesta_error(
                consulta,
                "respuesta_invalida",
                "El modelo devolvió una intención que el agente no reconoce",
            )

        return self._atender_herramienta(consulta, intencion)

    # --- Pasos internos ------------------------------------------------------

    def _atender_herramienta(self, consulta: str, intencion: Intencion) -> Respuesta:
        try:
            plan = planificar(
                self.ajustes,
                intencion.herramienta,
                intencion.argumentos,
                catalogo=self._catalogo,
            )
        except ErrorHerramienta as exc:
            return self._respuesta_error(
                consulta,
                exc.tipo,
                exc.mensaje,
                intencion=intencion,
                explicacion=exc.a_dict(),
            )

        self._mostrar_vista_previa(plan)

        aprobacion = None
        if plan.requiere_autorizacion:
            if not self._preguntar_aprobacion(plan):
                resultado = cancelar(plan)
                texto = "Modificación cancelada. No se envió nada al equipo."
                self._anotar(
                    consulta,
                    intencion=intencion,
                    plan=plan,
                    resultado=resultado,
                    nota=texto,
                )
                return Respuesta(
                    tipo=RESPUESTA_CANCELADO,
                    ok=False,
                    texto=texto,
                    resultado=resultado.a_dict(),
                    error=dict(resultado.error or {}),
                )
            aprobacion = aprobar(plan.accion)

        resultado = ejecutar(
            plan.accion,
            aprobacion,
            catalogo=self._catalogo,
            ajustes=self.ajustes,
        )
        texto = self._formatear_resultado(plan, resultado)
        self._anotar(consulta, intencion=intencion, plan=plan, resultado=resultado)

        if not resultado.ok:
            return Respuesta(
                tipo=RESPUESTA_ERROR,
                ok=False,
                texto=texto,
                resultado=resultado.a_dict(),
                error=dict(resultado.error or {}),
            )

        return Respuesta(
            tipo=RESPUESTA_MODIFICACION if plan.es_escritura else RESPUESTA_CONSULTA,
            ok=True,
            texto=texto,
            resultado=resultado.a_dict(),
        )

    def _preguntar_aprobacion(self, plan: Plan) -> bool:
        """Traduce la respuesta del operador a un sí o un no (nunca a otra cosa).

        Solo el valor ``True`` autoriza: una cadena, un número o cualquier otro
        objeto con valor de verdad se tratan como rechazo, para que ningún dato
        del modelo pueda colarse como aprobación.
        """
        try:
            return self._confirmar(plan) is True
        except (EOFError, KeyboardInterrupt):
            return False

    def _mostrar_vista_previa(self, plan: Plan) -> None:
        if not plan.es_escritura:
            self._salida(f"{plan.accion.descripcion}. No se modifica nada.")
            return

        vista = plan.vista_previa
        argumentos = dict(vista.get("argumentos") or {})
        lineas = [
            "Vista previa de la modificación (todavía no se envió nada al equipo):",
            f"  Equipo:            {vista.get('equipo', plan.accion.equipo)}",
            f"  Interfaz:          {argumentos.get('interfaz', '?')}",
            f"  Comentario nuevo:  «{argumentos.get('comentario', '')}»",
            f"  Efecto:            {vista.get('efecto', 'solo el comentario de la interfaz')}",
            "  Comentario actual: se lee al ejecutar (la vista previa no abre conexión).",
        ]
        self._salida("\n".join(lineas))

    @staticmethod
    def _verificado(salida: Mapping[str, Any]) -> bool:
        """Indica si la relectura del equipo confirma el cambio."""
        verificacion = salida.get("verificacion")
        if not isinstance(verificacion, Mapping):
            return False
        return bool(verificacion.get("ok")) and bool(verificacion.get("coincide"))

    @staticmethod
    def _linea_interfaz(datos: Mapping[str, Any]) -> str:
        partes = [str(datos.get("nombre", "?")).strip() or "?"]
        if datos.get("estado"):
            partes.append(str(datos["estado"]))
        if datos.get("ip"):
            partes.append(f"IP {datos['ip']}")
        if datos.get("comentario"):
            partes.append(f"«{datos['comentario']}»")
        return "  - " + " | ".join(partes)

    def _formatear_resultado(self, plan: Plan, resultado: Resultado) -> str:
        """Resumen en español que nunca afirma más de lo verificado."""
        salida = dict(resultado.salida or {})

        if not resultado.ok:
            error = dict(resultado.error or {})
            mensaje = error.get("mensaje", "falla no especificada")
            if error.get("tipo") == "verificacion_fallida":
                return (
                    f"El cambio no se pudo confirmar: {mensaje}. "
                    "No se informa como aplicado."
                )
            if plan.es_escritura:
                return f"No se aplicó la modificación: {mensaje}"
            return f"No se pudo completar la consulta: {mensaje}"

        if plan.es_escritura:
            if not self._verificado(salida):
                return (
                    "El equipo no confirmó el cambio al releer la interfaz; "
                    "no se informa como aplicado."
                )
            interfaz = plan.accion.interfaz
            equipo = plan.accion.equipo
            nuevo = salida.get("comentario_solicitado", "")
            anterior = salida.get("comentario_anterior")
            if not salida.get("changed"):
                return (
                    f"El comentario de {interfaz} en {equipo} ya era «{nuevo}»; "
                    "no se envió ningún cambio."
                )
            previo = "sin comentario" if not anterior else f"antes «{anterior}»"
            return (
                f"Listo: el comentario de {interfaz} en {equipo} quedó como «{nuevo}» "
                f"({previo}). La relectura de la interfaz coincide."
            )

        interfaces = salida.get("interfaces") or []
        if not interfaces:
            return "El equipo no devolvió interfaces reconocibles; no se informa un resultado inventado."
        equipo = salida.get("equipo") or plan.accion.equipo
        total = len(interfaces)
        activas = sum(1 for datos in interfaces if datos.get("estado") == "running")
        lineas = [f"Interfaces de {equipo} ({total}; {activas} en estado running):"]
        lineas.extend(self._linea_interfaz(datos) for datos in interfaces)
        return "\n".join(lineas)

    # --- Respuestas de error y registro --------------------------------------

    def _respuesta_limite(self, consulta: str) -> Respuesta:
        texto = (
            f"Se alcanzó el máximo de {self.ajustes.max_pasos} pasos de esta sesión. "
            "Escribe /salir y vuelve a iniciar para seguir trabajando."
        )
        motivo = error_json(
            "max_pasos_alcanzado",
            f"NETAGENT_MAX_PASOS={self.ajustes.max_pasos} ya consumidos",
        )
        self._anotar(
            consulta,
            nota="límite de pasos alcanzado: no se interpretó el mensaje",
            resultado={"ok": False, "error": dict(motivo)},
        )
        return Respuesta(tipo=RESPUESTA_ERROR, ok=False, texto=texto, error=motivo)

    def _respuesta_error(
        self,
        consulta: str,
        tipo: str,
        mensaje: str,
        *,
        intencion: Intencion | None = None,
        explicacion: Mapping[str, str] | None = None,
    ) -> Respuesta:
        texto = self._texto_error(tipo, mensaje)
        detalle = dict(explicacion) if explicacion is not None else error_json(tipo, mensaje)
        self._anotar(consulta, intencion=intencion, resultado={"ok": False, "error": detalle})
        return Respuesta(
            tipo=RESPUESTA_ERROR,
            ok=False,
            texto=texto,
            error=error_json(tipo, mensaje),
        )

    @staticmethod
    def _texto_error(tipo: str, mensaje: str) -> str:
        """Mensaje para la consola, sin datos técnicos innecesarios."""
        prefijos = {
            "configuracion": "Falta configuración",
            "autenticacion": "El proveedor de IA rechazó las credenciales",
            "conexion": "No se pudo conectar",
            "tiempo_agotado": "Tiempo agotado",
            "limite_tasa": "El proveedor limitó las solicitudes",
            "proveedor": "Falla del proveedor de IA",
            "respuesta_invalida": "El modelo devolvió una respuesta que no se puede usar",
            "herramienta_no_autorizada": "El modelo propuso una herramienta no autorizada",
            "argumentos_invalidos": "El modelo propuso argumentos no válidos",
            "dispositivo_no_autorizado": "Ese equipo no está autorizado",
            "interfaz_no_autorizada": "Esa interfaz no está autorizada",
            "parametros_invalidos": "La petición no es válida",
        }
        prefijo = prefijos.get(tipo, "No se pudo completar la operación")
        return f"{prefijo}: {mensaje}"

    def _anotar(
        self,
        consulta: str,
        *,
        intencion: Intencion | None = None,
        plan: Plan | None = None,
        resultado: Resultado | Mapping[str, Any] | None = None,
        nota: str | None = None,
    ) -> None:
        if self._registro is None:
            return

        evento: dict[str, Any] = {"usuario": consulta}
        if intencion is not None:
            evento["intencion"] = intencion.a_dict()
        if plan is not None:
            evento["plan"] = plan.a_dict()
        if resultado is not None:
            evento["resultado"] = (
                resultado.a_dict() if isinstance(resultado, Resultado) else dict(resultado)
            )
        if nota is not None:
            evento["nota"] = nota

        self._registro.anotar(evento)
        if self._registro.error and not self._aviso_registro_mostrado:
            self._aviso_registro_mostrado = True
            self._salida(
                "Aviso: no se pudo escribir el registro de la sesión "
                f"({self._registro.error})."
            )
