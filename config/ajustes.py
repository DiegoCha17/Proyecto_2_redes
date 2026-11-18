"""Carga y validación centralizada de la configuración de NetAgent-IA.

Responsabilidades:
  * Leer el inventario lógico (``config/dispositivos.yml``).
  * Leer las variables sensibles desde ``.env`` o desde el entorno del proceso.
  * Exponer objetos inmutables y ya validados al agente y a las herramientas.
  * Concentrar las reglas de seguridad que no dependen de la IA, como la
    política de bloqueo de direcciones IP.

Reglas de diseño:
  * Ninguna credencial se escribe en el código ni en el YAML versionado.
  * La validación falla cerrada: ante un dato inválido se lanza
    :class:`ErrorConfiguracion` y la operación no debe continuar.
  * Las credenciales nunca aparecen en ``repr()``/``str()`` para no filtrarse
    en mensajes, logs o trazas de error.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv

# --- Rutas canónicas del proyecto -------------------------------------------
RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
RUTA_DISPOSITIVOS = RAIZ_PROYECTO / "config" / "dispositivos.yml"
RUTA_ENV = RAIZ_PROYECTO / ".env"
DIRECTORIO_REGISTROS = RAIZ_PROYECTO / "registros"

# --- Valores por defecto ------------------------------------------------------
PLATAFORMAS_SOPORTADAS = frozenset({"mikrotik_routeros"})
MODELO_POR_DEFECTO = "gpt-4o-mini"
MAX_PASOS_POR_DEFECTO = 12


class ErrorConfiguracion(Exception):
    """Configuración ausente, incoherente o insegura."""


@dataclass(frozen=True)
class Dispositivo:
    """Dispositivo administrable del laboratorio (sin credenciales)."""

    clave: str
    nombre: str
    host: str
    rol: str
    descripcion: str
    plataforma: str
    prefijo_env: str
    puerto_ssh: int
    grupo_ansible: str
    interfaces_autorizadas: tuple[str, ...] = ()

    @property
    def usuario_env(self) -> str:
        """Nombre de la variable de entorno con el usuario SSH."""
        return f"{self.prefijo_env}_USUARIO"

    @property
    def clave_env(self) -> str:
        """Nombre de la variable de entorno con la contraseña SSH."""
        return f"{self.prefijo_env}_CLAVE"

    def interfaz_autorizada(self, interfaz: str) -> bool:
        """Indica si la interfaz pertenece al catálogo cerrado."""
        return interfaz in self.interfaces_autorizadas

    def resumen(self) -> str:
        """Texto corto para mensajes y registros."""
        return f"{self.nombre} ({self.rol}) en {self.host}"


@dataclass(frozen=True)
class Credenciales:
    """Credenciales SSH resueltas desde el entorno."""

    usuario: str
    clave: str = field(repr=False)
    puerto: int = 22

    def __str__(self) -> str:  # evita filtrar la clave en logs o excepciones
        return f"Credenciales(usuario={self.usuario!r}, clave=<oculta>)"


@dataclass(frozen=True)
class PoliticaBloqueo:
    """Catálogo cerrado de direcciones IP que el agente puede bloquear."""

    redes_permitidas: tuple[ipaddress.IPv4Network, ...]
    ips_protegidas: tuple[ipaddress.IPv4Address, ...]

    def validar_objetivo(self, ip: str) -> ipaddress.IPv4Address:
        """Valida que ``ip`` sea bloqueable y devuelve la dirección normalizada.

        Rechaza direcciones mal formadas, IPv6, infraestructura protegida y
        cualquier IP fuera de las redes autorizadas.
        """
        try:
            direccion = ipaddress.ip_address(str(ip).strip())
        except ValueError as exc:
            raise ErrorConfiguracion(f"Dirección IP inválida: {ip!r}") from exc

        if not isinstance(direccion, ipaddress.IPv4Address):
            raise ErrorConfiguracion(f"Solo se admite IPv4: {ip!r}")

        if direccion in self.ips_protegidas:
            raise ErrorConfiguracion(
                f"La IP {direccion} es infraestructura protegida y no puede bloquearse"
            )

        if not any(direccion in red for red in self.redes_permitidas):
            redes = ", ".join(str(red) for red in self.redes_permitidas)
            raise ErrorConfiguracion(
                f"La IP {direccion} está fuera de las redes autorizadas ({redes})"
            )

        return direccion

    def describir(self) -> dict[str, list[str]]:
        """Representación serializable de la política (para logs y respuestas)."""
        return {
            "redes_permitidas": [str(red) for red in self.redes_permitidas],
            "ips_protegidas": [str(ip) for ip in self.ips_protegidas],
        }


@dataclass(frozen=True)
class Ajustes:
    """Configuración completa del proyecto, ya validada."""

    laboratorio: Mapping[str, Any]
    dispositivos: Mapping[str, Dispositivo]
    politica_bloqueo: PoliticaBloqueo
    modelo: str
    max_pasos: int
    requiere_aprobacion: bool
    ruta_env: Path

    def dispositivo(self, clave: str) -> Dispositivo:
        """Devuelve un dispositivo por su clave o lanza error si no existe."""
        try:
            return self.dispositivos[clave.lower()]
        except KeyError:
            validos = ", ".join(sorted(self.dispositivos))
            raise ErrorConfiguracion(
                f"Dispositivo desconocido: {clave!r}. Válidos: {validos}"
            ) from None

    def credenciales(self, clave: str) -> Credenciales:
        """Resuelve las credenciales SSH de un dispositivo desde el entorno."""
        dispositivo = self.dispositivo(clave)
        faltantes = [
            nombre
            for nombre in (dispositivo.usuario_env, dispositivo.clave_env)
            if not os.environ.get(nombre)
        ]
        if faltantes:
            raise ErrorConfiguracion(
                f"Faltan variables de entorno para {dispositivo.nombre}: "
                f"{', '.join(faltantes)}. Copie .env.example a .env y complételas."
            )

        return Credenciales(
            usuario=os.environ[dispositivo.usuario_env].strip(),
            clave=os.environ[dispositivo.clave_env],
            puerto=dispositivo.puerto_ssh,
        )

    def variables_entorno_faltantes(self) -> list[str]:
        """Lista (sin valores) las variables de entorno aún no configuradas."""
        faltantes: list[str] = []
        for dispositivo in self.dispositivos.values():
            for nombre in (dispositivo.usuario_env, dispositivo.clave_env):
                if not os.environ.get(nombre):
                    faltantes.append(nombre)
        if not os.environ.get("OPENAI_API_KEY"):
            faltantes.append("OPENAI_API_KEY")
        return faltantes


# --- Lectura y validación del YAML -------------------------------------------


def _leer_yaml(ruta: Path) -> dict[str, Any]:
    if not ruta.is_file():
        raise ErrorConfiguracion(f"No existe el archivo de configuración: {ruta}")
    try:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ErrorConfiguracion(f"No se pudo leer {ruta}: {exc}") from exc
    if not isinstance(datos, dict):
        raise ErrorConfiguracion(f"El archivo {ruta} debe contener un mapa YAML")
    return datos


def _texto(datos: Mapping[str, Any], clave: str, contexto: str) -> str:
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor.strip():
        raise ErrorConfiguracion(f"{contexto}: el campo '{clave}' es obligatorio")
    return valor.strip()


def _puerto(valor: Any, contexto: str) -> int:
    if not isinstance(valor, int) or isinstance(valor, bool) or not 1 <= valor <= 65535:
        raise ErrorConfiguracion(f"{contexto}: 'puerto_ssh' debe ser un entero 1-65535")
    return valor


def _construir_dispositivo(clave: str, datos: Any) -> Dispositivo:
    contexto = f"dispositivos.{clave}"
    if not isinstance(datos, dict):
        raise ErrorConfiguracion(f"{contexto}: debe ser un mapa con los datos del equipo")

    host = _texto(datos, "host", contexto)
    try:
        ipaddress.IPv4Address(host)
    except ValueError as exc:
        raise ErrorConfiguracion(f"{contexto}: 'host' no es una IPv4 válida: {host!r}") from exc

    plataforma = _texto(datos, "plataforma", contexto)
    if plataforma not in PLATAFORMAS_SOPORTADAS:
        soportadas = ", ".join(sorted(PLATAFORMAS_SOPORTADAS))
        raise ErrorConfiguracion(
            f"{contexto}: plataforma {plataforma!r} no autorizada (válidas: {soportadas})"
        )

    interfaces = datos.get("interfaces_autorizadas", [])
    if not isinstance(interfaces, list) or not all(
        isinstance(item, str) and item.strip() for item in interfaces
    ):
        raise ErrorConfiguracion(
            f"{contexto}: 'interfaces_autorizadas' debe ser una lista de nombres"
        )
    interfaces_normalizadas = tuple(item.strip() for item in interfaces)
    if len(set(interfaces_normalizadas)) != len(interfaces_normalizadas):
        raise ErrorConfiguracion(f"{contexto}: hay interfaces repetidas en el catálogo")

    return Dispositivo(
        clave=clave,
        nombre=_texto(datos, "nombre", contexto),
        host=host,
        rol=_texto(datos, "rol", contexto),
        descripcion=str(datos.get("descripcion", "")).strip(),
        plataforma=plataforma,
        prefijo_env=_texto(datos, "prefijo_env", contexto),
        puerto_ssh=_puerto(datos.get("puerto_ssh", 22), contexto),
        grupo_ansible=_texto(datos, "grupo_ansible", contexto),
        interfaces_autorizadas=interfaces_normalizadas,
    )


def _construir_politica(datos: Any) -> PoliticaBloqueo:
    if not isinstance(datos, dict):
        raise ErrorConfiguracion("Falta la sección 'politica_bloqueo'")

    redes_crudas = datos.get("redes_permitidas")
    if not isinstance(redes_crudas, list) or not redes_crudas:
        raise ErrorConfiguracion(
            "politica_bloqueo.redes_permitidas debe ser una lista no vacía"
        )
    redes: list[ipaddress.IPv4Network] = []
    for red in redes_crudas:
        try:
            redes.append(ipaddress.IPv4Network(str(red).strip(), strict=False))
        except ValueError as exc:
            raise ErrorConfiguracion(
                f"politica_bloqueo: red inválida {red!r}"
            ) from exc

    ips_crudas = datos.get("ips_protegidas", [])
    if not isinstance(ips_crudas, list):
        raise ErrorConfiguracion("politica_bloqueo.ips_protegidas debe ser una lista")
    protegidas: list[ipaddress.IPv4Address] = []
    for ip in ips_crudas:
        try:
            protegidas.append(ipaddress.IPv4Address(str(ip).strip()))
        except ValueError as exc:
            raise ErrorConfiguracion(
                f"politica_bloqueo: IP protegida inválida {ip!r}"
            ) from exc

    return PoliticaBloqueo(
        redes_permitidas=tuple(redes),
        ips_protegidas=tuple(protegidas),
    )


# --- Variables de entorno opcionales ----------------------------------------


def _entero_entorno(nombre: str, defecto: int, minimo: int, maximo: int) -> int:
    crudo = os.environ.get(nombre, "").strip()
    if not crudo:
        return defecto
    try:
        valor = int(crudo)
    except ValueError as exc:
        raise ErrorConfiguracion(f"{nombre} debe ser un número entero") from exc
    if not minimo <= valor <= maximo:
        raise ErrorConfiguracion(f"{nombre} debe estar entre {minimo} y {maximo}")
    return valor


def _booleano_entorno(nombre: str, defecto: bool) -> bool:
    crudo = os.environ.get(nombre, "").strip().lower()
    if not crudo:
        return defecto
    if crudo in {"1", "true", "si", "sí", "yes", "on"}:
        return True
    if crudo in {"0", "false", "no", "off"}:
        return False
    raise ErrorConfiguracion(f"{nombre} debe ser true/false")


def cargar_ajustes(
    ruta_dispositivos: Path | None = None,
    ruta_env: Path | None = None,
) -> Ajustes:
    """Carga y valida toda la configuración del proyecto.

    ``override=False`` al cargar ``.env``: una variable ya exportada en el
    entorno tiene prioridad sobre el archivo.
    """
    ruta_env = ruta_env or RUTA_ENV
    ruta_dispositivos = ruta_dispositivos or RUTA_DISPOSITIVOS

    if ruta_env.is_file():
        load_dotenv(dotenv_path=ruta_env, override=False)

    datos = _leer_yaml(ruta_dispositivos)

    laboratorio = datos.get("laboratorio")
    if not isinstance(laboratorio, dict):
        raise ErrorConfiguracion("Falta la sección 'laboratorio' en dispositivos.yml")

    dispositivos_crudos = datos.get("dispositivos")
    if not isinstance(dispositivos_crudos, dict) or not dispositivos_crudos:
        raise ErrorConfiguracion("'dispositivos' debe ser un mapa no vacío")

    dispositivos: dict[str, Dispositivo] = {}
    for clave, contenido in dispositivos_crudos.items():
        clave_normalizada = str(clave).strip().lower()
        dispositivo = _construir_dispositivo(clave_normalizada, contenido)
        if dispositivo.host in {d.host for d in dispositivos.values()}:
            raise ErrorConfiguracion(
                f"La IP {dispositivo.host} está asignada a más de un dispositivo"
            )
        dispositivos[clave_normalizada] = dispositivo

    politica = _construir_politica(datos.get("politica_bloqueo"))

    modelo = os.environ.get("NETAGENT_MODELO", "").strip() or MODELO_POR_DEFECTO

    return Ajustes(
        laboratorio=dict(laboratorio),
        dispositivos=dispositivos,
        politica_bloqueo=politica,
        modelo=modelo,
        max_pasos=_entero_entorno("NETAGENT_MAX_PASOS", MAX_PASOS_POR_DEFECTO, 1, 50),
        requiere_aprobacion=_booleano_entorno("NETAGENT_REQUIERE_APROBACION", True),
        ruta_env=ruta_env,
    )


@lru_cache(maxsize=1)
def obtener_ajustes() -> Ajustes:
    """Configuración compartida (cargada una vez por proceso)."""
    return cargar_ajustes()


def recargar_ajustes() -> Ajustes:
    """Fuerza una nueva lectura de ``.env`` y del inventario."""
    obtener_ajustes.cache_clear()
    return obtener_ajustes()
