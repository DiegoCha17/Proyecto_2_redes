#!/usr/bin/env python3
"""Genera ``ansible/inventario.ini`` desde ``config/dispositivos.yml``.

El inventario de Ansible se deriva del YAML para no duplicar direcciones IP.
No incluye credenciales: usa ``prefijo_env`` y ``ansible/group_vars/mikrotik.yml``
las resuelve desde el entorno en tiempo de ejecución.

Uso:
    python3 scripts/generar_inventario.py             # escribe el inventario
    python3 scripts/generar_inventario.py --verificar # solo comprueba sincronía
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from config.ajustes import Ajustes, ErrorConfiguracion, cargar_ajustes  # noqa: E402

RUTA_INVENTARIO = RAIZ / "ansible" / "inventario.ini"

ENCABEZADO = """\
# =============================================================================
# ARCHIVO GENERADO AUTOMÁTICAMENTE - NO EDITAR A MANO
#
# Fuente: config/dispositivos.yml
# Regenerar con: python3 scripts/generar_inventario.py
#
# No contiene credenciales. ansible/group_vars/mikrotik.yml resuelve
# usuario y contraseña desde las variables de entorno del proyecto.
# =============================================================================
"""


def generar_contenido(ajustes: Ajustes) -> str:
    """Devuelve el texto del inventario INI ordenado por grupo y host."""
    grupos: dict[str, list] = defaultdict(list)
    for dispositivo in ajustes.dispositivos.values():
        grupos[dispositivo.grupo_ansible].append(dispositivo)

    lineas = [ENCABEZADO]
    for grupo in sorted(grupos):
        lineas.append(f"[{grupo}]")
        for dispositivo in sorted(grupos[grupo], key=lambda d: d.clave):
            lineas.append(
                f"{dispositivo.clave} "
                f"ansible_host={dispositivo.host} "
                f"prefijo_env={dispositivo.prefijo_env}"
            )
        lineas.append("")

    return "\n".join(lineas).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el inventario de Ansible.")
    parser.add_argument(
        "--verificar",
        action="store_true",
        help="no escribe: comprueba que el inventario esté sincronizado",
    )
    parser.add_argument(
        "--salida",
        type=Path,
        default=RUTA_INVENTARIO,
        help=f"ruta del inventario (por defecto {RUTA_INVENTARIO})",
    )
    args = parser.parse_args(argv)

    try:
        ajustes = cargar_ajustes()
    except ErrorConfiguracion as exc:
        print(f"FALLA de configuración: {exc}", file=sys.stderr)
        return 2

    contenido = generar_contenido(ajustes)

    if args.verificar:
        if not args.salida.is_file():
            print(f"FALLA: no existe {args.salida}; ejecute scripts/generar_inventario.py")
            return 1
        try:
            actual = args.salida.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"FALLA: no se pudo leer {args.salida}: {exc}", file=sys.stderr)
            return 1
        if actual != contenido:
            print(
                "FALLA: el inventario está desactualizado respecto de "
                "config/dispositivos.yml; ejecute scripts/generar_inventario.py"
            )
            return 1
        print(f"OK: {args.salida} está sincronizado con config/dispositivos.yml")
        return 0

    try:
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(contenido, encoding="utf-8")
    except OSError as exc:
        print(f"FALLA: no se pudo escribir {args.salida}: {exc}", file=sys.stderr)
        return 2

    resumen = ", ".join(
        f"{d.nombre}={d.host}" for d in sorted(ajustes.dispositivos.values(), key=lambda d: d.clave)
    )
    print(f"Inventario generado: {args.salida}")
    print(f"Hosts: {resumen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
