#!/usr/bin/env python3
"""Ejecuta un programa local con credenciales NetAgent obtenidas de Vault.

Uso: python scripts/ejecutar_con_vault.py -- python main.py
El archivo Vault se descifra en memoria; nunca se escriben sus contenidos.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import yaml

RAIZ = Path(__file__).resolve().parents[1]
VAULT = RAIZ / 'ansible/vault/vault.yml'


class ErrorVault(Exception):
    """Error que no contiene ni el contenido del vault ni la salida del proceso."""


class CargadorSinDuplicados(yaml.SafeLoader):
    pass


def _mapa_unico(cargador, nodo):
    resultado = {}
    for clave_nodo, valor_nodo in nodo.value:
        clave = cargador.construct_object(clave_nodo)
        if not isinstance(clave, str) or clave in resultado:
            raise ErrorVault('Vault contiene claves inválidas o duplicadas.')
        resultado[clave] = cargador.construct_object(valor_nodo)
    return resultado


CargadorSinDuplicados.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapa_unico)


def credenciales_vault(ruta: Path, entorno: dict[str, str]) -> dict[str, str]:
    """Descifra y valida el esquema cerrado de credenciales para ambos equipos."""
    password_file = entorno.get('ANSIBLE_VAULT_PASSWORD_FILE', '').strip()
    if not password_file:
        raise ErrorVault('Configure ANSIBLE_VAULT_PASSWORD_FILE con la ruta local de la contraseña maestra.')
    if Path(password_file).resolve().is_relative_to(RAIZ.resolve()):
        raise ErrorVault('La contraseña maestra debe estar fuera del repositorio.')
    entorno_descifrado = entorno.copy()
    # La ruta se entrega una sola vez: evita identidades default duplicadas.
    entorno_descifrado.pop('ANSIBLE_VAULT_PASSWORD_FILE', None)
    try:
        with ruta.open('rb') as archivo:
            if not archivo.readline().startswith(b'$ANSIBLE_VAULT;'):
                raise ErrorVault('El archivo de credenciales no está cifrado con Ansible Vault.')
        resultado = subprocess.run(
            ['ansible-vault', 'decrypt', '--vault-password-file', password_file,
             '--output', '-', str(ruta)],
            cwd=RAIZ, env=entorno_descifrado, capture_output=True, text=True,
            encoding='utf-8', check=False, timeout=30,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ErrorVault('No se pudo abrir el archivo cifrado o ejecutar ansible-vault.') from exc
    if resultado.returncode != 0:
        raise ErrorVault('No se pudo descifrar Vault; revise el archivo y la contraseña maestra.')
    try:
        datos = yaml.load(resultado.stdout, Loader=CargadorSinDuplicados)
    except yaml.YAMLError as exc:
        raise ErrorVault('El contenido de Vault no tiene un formato YAML válido.') from exc
    if not isinstance(datos, dict) or set(datos) != {'r1', 'sw1'}:
        raise ErrorVault('Vault debe contener únicamente las secciones r1 y sw1.')
    variables = {}
    for equipo in ('r1', 'sw1'):
        campos = datos[equipo]
        if (not isinstance(campos, dict) or set(campos) != {'usuario', 'clave'}
                or any(not isinstance(campos[campo], str) or not campos[campo].strip()
                       or '\x00' in campos[campo] or '\n' in campos[campo] or '\r' in campos[campo]
                       for campo in ('usuario', 'clave'))):
            raise ErrorVault('Cada equipo debe tener usuario y clave de texto no vacíos.')
        prefijo = 'NETAGENT_' + equipo.upper()
        variables[prefijo + '_USUARIO'] = campos['usuario']
        variables[prefijo + '_CLAVE'] = campos['clave']
    return variables


def main(argv: list[str] | None = None) -> int:
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if argumentos and argumentos[0] == '--':
        argumentos.pop(0)
    if not argumentos:
        print('Uso: python scripts/ejecutar_con_vault.py -- python main.py', file=sys.stderr)
        return 2
    entorno = os.environ.copy()
    try:
        entorno.update(credenciales_vault(VAULT, entorno))
        entorno.setdefault('ANSIBLE_CONFIG', str(RAIZ / 'ansible/ansible.cfg'))
        proceso = subprocess.run(argumentos, cwd=RAIZ, env=entorno, check=False)
    except ErrorVault as exc:
        print(f'Error de Vault: {exc}', file=sys.stderr)
        return 1
    except OSError:
        print('No se pudo iniciar el programa solicitado.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return proceso.returncode if proceso.returncode >= 0 else 128 - proceso.returncode


if __name__ == '__main__':
    raise SystemExit(main())
