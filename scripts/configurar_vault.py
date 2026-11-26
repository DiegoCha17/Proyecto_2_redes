#!/usr/bin/env python3
"""Crea o reconstruye el Vault de NetAgent sin abrir un editor.

Las credenciales se solicitan en terminal, se cifran desde memoria y nunca se
muestran. Los equipos no son consultados ni modificados.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime

import yaml

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts.ejecutar_con_vault import ErrorVault, VAULT, credenciales_vault  # noqa: E402


def _validar_maestra(ruta: str) -> None:
    if not ruta:
        raise ErrorVault('Configure ANSIBLE_VAULT_PASSWORD_FILE antes de continuar.')
    archivo = Path(ruta).expanduser().resolve()
    if archivo.is_relative_to(RAIZ.resolve()) or not archivo.is_file():
        raise ErrorVault('La contraseña maestra debe ser un archivo existente fuera del repositorio.')


def _pedir_dato(etiqueta: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        primero = getpass.getpass(f'{etiqueta}: ')
        segundo = getpass.getpass(f'Confirme {etiqueta.lower()}: ')
    if (primero != segundo or not primero.strip()
            or any(c in primero for c in '\x00\r\n')):
        raise ErrorVault('Los datos no coinciden o contienen caracteres no admitidos; no se modificó el Vault.')
    return primero


def configurar(ruta: Path, maestra: str, credenciales: dict, entorno: dict[str, str]) -> Path | None:
    """Cifra, verifica mediante el lanzador y reemplaza solo tras respaldar."""
    _validar_maestra(maestra)
    if set(credenciales) != {'r1', 'sw1'}:
        raise ErrorVault('Se requieren credenciales separadas de R1 y SW1.')
    for equipo in ('r1', 'sw1'):
        datos = credenciales[equipo]
        if (not isinstance(datos, dict) or set(datos) != {'usuario', 'clave'}
                or any(not isinstance(datos[k], str) or not datos[k].strip()
                       or any(c in datos[k] for c in '\x00\r\n') for k in ('usuario', 'clave'))
                or datos['usuario'] != datos['usuario'].strip()):
            raise ErrorVault('Las credenciales deben ser texto válido y los usuarios no deben tener espacios exteriores.')

    ruta = Path(ruta)
    if ruta.is_symlink():
        raise ErrorVault('El archivo Vault no puede ser un enlace simbólico.')
    ruta.parent.mkdir(parents=True, exist_ok=True)
    anterior = ruta.read_bytes() if ruta.exists() else None
    if anterior is not None and not anterior.startswith(b'$ANSIBLE_VAULT;'):
        raise ErrorVault('El Vault existente no está cifrado; no se sobrescribirá.')

    # Evitar que el mismo archivo sea agregado dos veces con identidad default.
    entorno_cifrado = entorno.copy()
    entorno_cifrado.pop('ANSIBLE_VAULT_PASSWORD_FILE', None)
    with tempfile.TemporaryDirectory(prefix='.vault-config-', dir=ruta.parent) as directorio:
        nuevo = Path(directorio) / 'vault.yml'
        texto = yaml.safe_dump(credenciales, allow_unicode=True, sort_keys=False)
        try:
            cifrado = subprocess.run(
                ['ansible-vault', 'encrypt', '--vault-password-file', maestra,
                 '--output', str(nuevo), '-'], input=texto, text=True, capture_output=True,
                cwd=RAIZ, env=entorno_cifrado, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ErrorVault('No se pudo ejecutar el cifrado de Ansible Vault.') from exc
        if cifrado.returncode != 0 or not nuevo.is_file() or not nuevo.read_bytes().startswith(b'$ANSIBLE_VAULT;'):
            raise ErrorVault('Falló el cifrado; el Vault existente no se modificó.')
        os.chmod(nuevo, 0o600)

        entorno_verificacion = entorno.copy()
        entorno_verificacion['ANSIBLE_VAULT_PASSWORD_FILE'] = maestra
        valores = credenciales_vault(nuevo, entorno_verificacion)
        esperados = {f'NETAGENT_{e.upper()}_{campo}': credenciales[e][clave]
                     for e in ('r1', 'sw1') for campo, clave in (('USUARIO', 'usuario'), ('CLAVE', 'clave'))}
        if valores != esperados:
            diferentes = [nombre for nombre in esperados if valores.get(nombre) != esperados[nombre]]
            detalle = ', '.join(diferentes) if diferentes else 'esquema inesperado'
            raise ErrorVault(f'La comprobación exacta falló ({detalle}); el Vault existente no se modificó.')

        respaldo = None
        if anterior is not None:
            marca = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            respaldo = ruta.with_name(ruta.name + '.bak-' + marca)
            fd = os.open(respaldo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as archivo:
                archivo.write(anterior)
                archivo.flush()
                os.fsync(archivo.fileno())
            if respaldo.read_bytes() != anterior or ruta.is_symlink() or ruta.read_bytes() != anterior:
                raise ErrorVault('El respaldo o el archivo original cambió; no se realizará la sustitución.')
        elif ruta.exists() or ruta.is_symlink():
            raise ErrorVault('Apareció un archivo Vault durante la operación; no se sobrescribirá.')
        with nuevo.open('rb') as archivo:
            os.fsync(archivo.fileno())
        os.replace(nuevo, ruta)
        return respaldo


def main() -> int:
    entorno = os.environ.copy()
    maestra = entorno.get('ANSIBLE_VAULT_PASSWORD_FILE', '').strip()
    try:
        _validar_maestra(maestra)
        credenciales = {}
        for equipo in ('r1', 'sw1'):
            print(f'Introduzca los datos SSH de {equipo.upper()} (no se mostrarán).')
            credenciales[equipo] = {
                'usuario': _pedir_dato(f'Usuario de {equipo.upper()}'),
                'clave': _pedir_dato(f'Contraseña de {equipo.upper()}'),
            }
        respaldo = configurar(VAULT, maestra, credenciales, entorno)
        if respaldo:
            print(f'Respaldo cifrado creado: {respaldo}')
        print('Vault cifrado y verificado. No se modificaron los dispositivos.')
        return 0
    except ErrorVault as exc:
        print(f'No se pudo configurar Vault: {exc}', file=sys.stderr)
    except (OSError, yaml.YAMLError):
        print('No se pudo configurar Vault por un error local; no se mostrarán detalles sensibles.', file=sys.stderr)
    except (KeyboardInterrupt, EOFError, getpass.GetPassWarning):
        print('\nOperación cancelada; no se modificaron los dispositivos.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
