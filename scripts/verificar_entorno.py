#!/usr/bin/env python3
"""Comprueba el entorno de desarrollo del Proyecto 2 (solo lectura).

Valida, sin modificar nada:
  1. Versión de Python.
  2. Dependencias Python (Netmiko, OpenAI, python-dotenv, PyYAML).
  3. Ansible y las colecciones requeridas (ansible.netcommon, community.routeros).
  4. Existencia y validez de ``.env`` y de la configuración del proyecto.
  5. Sincronía entre ``config/dispositivos.yml`` y el inventario de Ansible.
  6. Conectividad TCP al puerto SSH de R1 y SW1 (opcional: --sin-red).

La comprobación de red es una conexión TCP; no envía comandos a los equipos
ni altera GNS3 ni la configuración de los MikroTik.

Uso:
    python3 scripts/verificar_entorno.py
    python3 scripts/verificar_entorno.py --sin-red
    python3 scripts/verificar_entorno.py --estricto
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

PYTHON_MINIMO = (3, 10)
DEPENDENCIAS = {
    "netmiko": "netmiko",
    "openai": "openai",
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
}
COLECCIONES_REQUERIDAS = ("ansible.netcommon", "community.routeros")

OK = "OK"
AVISO = "AVISO"
FALLA = "FALLA"


@dataclass
class Comprobacion:
    """Resultado de una comprobación individual."""

    nombre: str
    estado: str
    detalle: str


def _version_distribucion(distribucion: str) -> str:
    try:
        return metadata.version(distribucion)
    except metadata.PackageNotFoundError:
        return "?"


def comprobar_python() -> Comprobacion:
    actual = sys.version_info
    version = platform.python_version()
    if actual[:2] < PYTHON_MINIMO:
        minimo = ".".join(map(str, PYTHON_MINIMO))
        return Comprobacion("Python", FALLA, f"{version}; se requiere >= {minimo}")
    return Comprobacion("Python", OK, f"{version} ({sys.executable})")


def comprobar_dependencias() -> list[Comprobacion]:
    resultados = []
    for modulo, distribucion in DEPENDENCIAS.items():
        if importlib.util.find_spec(modulo) is None:
            resultados.append(
                Comprobacion(f"Dependencia {distribucion}", FALLA, "no instalada")
            )
        else:
            resultados.append(
                Comprobacion(
                    f"Dependencia {distribucion}",
                    OK,
                    f"versión {_version_distribucion(distribucion)}",
                )
            )
    return resultados


def comprobar_ansible() -> list[Comprobacion]:
    ejecutable = shutil.which("ansible")
    if not ejecutable:
        return [
            Comprobacion("Ansible", FALLA, "no está instalado o no está en PATH"),
            Comprobacion("Colecciones Ansible", FALLA, "no se pudo comprobar sin Ansible"),
        ]

    try:
        proceso = subprocess.run(
            ["ansible", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            Comprobacion("Ansible", FALLA, f"error al ejecutar ansible: {exc}"),
            Comprobacion("Colecciones Ansible", FALLA, "no se pudo comprobar sin Ansible"),
        ]

    if proceso.returncode != 0:
        detalle = (proceso.stderr or proceso.stdout).strip().splitlines()
        return [
            Comprobacion("Ansible", FALLA, detalle[0] if detalle else "ansible --version falló"),
            Comprobacion("Colecciones Ansible", FALLA, "no se pudo comprobar sin Ansible"),
        ]

    primera_linea = proceso.stdout.strip().splitlines()[0] if proceso.stdout.strip() else "?"
    resultados = [Comprobacion("Ansible", OK, primera_linea)]

    try:
        listado = subprocess.run(
            ["ansible-galaxy", "collection", "list"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        resultados.append(Comprobacion("Colecciones Ansible", FALLA, f"error: {exc}"))
        return resultados

    salida = listado.stdout
    if listado.returncode != 0:
        resultados.append(
            Comprobacion("Colecciones Ansible", FALLA, "ansible-galaxy collection list falló")
        )
        return resultados

    for coleccion in COLECCIONES_REQUERIDAS:
        if coleccion in salida:
            resultados.append(Comprobacion(f"Colección {coleccion}", OK, "instalada"))
        else:
            resultados.append(
                Comprobacion(
                    f"Colección {coleccion}",
                    FALLA,
                    f"no instalada; ejecute: ansible-galaxy collection install {coleccion}",
                )
            )
    return resultados


def comprobar_configuracion() -> tuple[list[Comprobacion], object | None]:
    """Carga la configuración del proyecto y revisa variables de entorno."""
    resultados: list[Comprobacion] = []
    try:
        from config.ajustes import ErrorConfiguracion, RUTA_ENV, cargar_ajustes
    except Exception as exc:  # dependencias rotas o proyecto incompleto
        return [
            Comprobacion("Módulo de configuración", FALLA, f"no se pudo importar: {exc}")
        ], None

    if RUTA_ENV.is_file():
        resultados.append(Comprobacion("Archivo .env", OK, f"presente ({RUTA_ENV.name})"))
    else:
        resultados.append(
            Comprobacion(
                "Archivo .env",
                AVISO,
                "no existe; copie .env.example a .env y complete las credenciales",
            )
        )
    if RUTA_ENV.is_file() and (RUTA_ENV.stat().st_mode & 0o077):
        resultados.append(
            Comprobacion(
                "Permisos de .env",
                AVISO,
                f"permisos {oct(RUTA_ENV.stat().st_mode & 0o777)}; se recomienda chmod 600 .env",
            )
        )

    try:
        ajustes = cargar_ajustes()
    except ErrorConfiguracion as exc:
        resultados.append(Comprobacion("Inventario dispositivos.yml", FALLA, str(exc)))
        return resultados, None

    dispositivos = ", ".join(
        f"{d.nombre}={d.host}" for d in sorted(ajustes.dispositivos.values(), key=lambda d: d.clave)
    )
    resultados.append(
        Comprobacion(
            "Inventario dispositivos.yml",
            OK,
            f"{len(ajustes.dispositivos)} dispositivos: {dispositivos}",
        )
    )

    politica = ajustes.politica_bloqueo
    resultados.append(
        Comprobacion(
            "Política de bloqueo",
            OK,
            f"{len(politica.redes_permitidas)} red(es) autorizada(s), "
            f"{len(politica.ips_protegidas)} IP(s) protegida(s)",
        )
    )

    faltantes = ajustes.variables_entorno_faltantes()
    if faltantes:
        resultados.append(
            Comprobacion(
                "Credenciales y API key",
                AVISO,
                "sin configurar: " + ", ".join(faltantes),
            )
        )
    else:
        resultados.append(
            Comprobacion("Credenciales y API key", OK, "todas las variables están definidas")
        )

    return resultados, ajustes


def comprobar_inventario_ansible() -> list[Comprobacion]:
    resultados: list[Comprobacion] = []
    try:
        from scripts.generar_inventario import RUTA_INVENTARIO, generar_contenido
        from config.ajustes import cargar_ajustes
    except Exception as exc:
        return [Comprobacion("Inventario Ansible", FALLA, f"no se pudo importar: {exc}")]

    try:
        contenido = generar_contenido(cargar_ajustes())
    except Exception as exc:
        return [Comprobacion("Inventario Ansible", FALLA, f"configuración inválida: {exc}")]

    if not RUTA_INVENTARIO.is_file():
        resultados.append(
            Comprobacion(
                "Inventario Ansible",
                FALLA,
                f"no existe {RUTA_INVENTARIO}; ejecute scripts/generar_inventario.py",
            )
        )
        return resultados

    if RUTA_INVENTARIO.read_text(encoding="utf-8") != contenido:
        resultados.append(
            Comprobacion(
                "Inventario Ansible",
                FALLA,
                "desactualizado respecto de config/dispositivos.yml",
            )
        )
        return resultados

    resultados.append(Comprobacion("Inventario Ansible", OK, "sincronizado con el YAML"))

    # Valida que Ansible pueda interpretar el inventario generado.
    try:
        proceso = subprocess.run(
            ["ansible-inventory", "-i", str(RUTA_INVENTARIO), "--list"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            cwd=str(RUTA_INVENTARIO.parent),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        resultados.append(Comprobacion("Parseo del inventario", FALLA, f"error: {exc}"))
        return resultados

    if proceso.returncode != 0:
        error = (proceso.stderr or proceso.stdout).strip().splitlines()
        resultados.append(
            Comprobacion(
                "Parseo del inventario",
                FALLA,
                error[-1] if error else "ansible-inventory falló",
            )
        )
    else:
        resultados.append(
            Comprobacion("Parseo del inventario", OK, "ansible-inventory lo interpreta")
        )
    return resultados


def comprobar_red(ajustes) -> list[Comprobacion]:
    """Comprueba el puerto SSH con TCP; no envía comandos ni modifica equipos."""
    resultados = []
    for dispositivo in sorted(ajustes.dispositivos.values(), key=lambda d: d.clave):
        try:
            with socket.create_connection(
                (dispositivo.host, dispositivo.puerto_ssh), timeout=2
            ):
                resultados.append(
                    Comprobacion(
                        f"SSH {dispositivo.nombre}",
                        OK,
                        f"{dispositivo.host}:{dispositivo.puerto_ssh} responde",
                    )
                )
        except OSError as exc:
            resultados.append(
                Comprobacion(
                    f"SSH {dispositivo.nombre}",
                    AVISO,
                    f"{dispositivo.host}:{dispositivo.puerto_ssh} sin respuesta ({exc}); "
                    "verifique que la topología GNS3 esté iniciada",
                )
            )
    return resultados


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica el entorno de desarrollo de NetAgent-IA."
    )
    parser.add_argument(
        "--sin-red", action="store_true", help="omite la comprobación de conectividad SSH"
    )
    parser.add_argument(
        "--estricto",
        action="store_true",
        help="considera los avisos como fallas (código de salida 1)",
    )
    args = parser.parse_args(argv)

    comprobaciones: list[Comprobacion] = [comprobar_python()]
    comprobaciones.extend(comprobar_dependencias())
    comprobaciones.extend(comprobar_ansible())

    config_resultados, ajustes = comprobar_configuracion()
    comprobaciones.extend(config_resultados)
    comprobaciones.extend(comprobar_inventario_ansible())
    if ajustes is not None and not args.sin_red:
        comprobaciones.extend(comprobar_red(ajustes))

    print("=" * 72)
    print("Verificación de entorno - NetAgent-IA (P2, Grupo 6)")
    print("=" * 72)
    for comprobacion in comprobaciones:
        print(f"[{comprobacion.estado:<5}] {comprobacion.nombre}: {comprobacion.detalle}")

    total_ok = sum(1 for c in comprobaciones if c.estado == OK)
    total_avisos = sum(1 for c in comprobaciones if c.estado == AVISO)
    total_fallas = sum(1 for c in comprobaciones if c.estado == FALLA)

    if total_fallas:
        global_estado = "fallas"
    elif total_avisos:
        global_estado = "advertencias"
    else:
        global_estado = "ok"

    print("-" * 72)
    print(
        f"Resultado: {global_estado.upper()} "
        f"({total_ok} OK, {total_avisos} avisos, {total_fallas} fallas)"
    )

    informe = {
        "herramienta": "verificar_entorno",
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sistema": {
            "so": platform.platform(),
            "python": platform.python_version(),
            "usuario": os.environ.get("USER", "?"),
        },
        "resultado_global": global_estado,
        "resumen": {"ok": total_ok, "avisos": total_avisos, "fallas": total_fallas},
        "comprobaciones": [asdict(c) for c in comprobaciones],
    }

    ruta_informe = RAIZ / "registros" / "verificacion_entorno.json"
    try:
        ruta_informe.parent.mkdir(parents=True, exist_ok=True)
        ruta_informe.write_text(
            json.dumps(informe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Informe JSON: {ruta_informe}")
    except OSError as exc:
        print(f"AVISO: no se pudo escribir el informe JSON: {exc}")
        return 1

    if total_fallas:
        return 1
    if args.estricto and total_avisos:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
