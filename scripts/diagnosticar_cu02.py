#!/usr/bin/env python3
"""Diagnóstico de las lecturas CU-02 en R1, sin cambios en el firewall.

Ejecuta copias temporales de las tareas de lectura y procesamiento del playbook
original. Nunca imprime la salida cruda ni cambia la protección no_log.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types

import yaml
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[1]
NOMBRES = (
    'Validar parámetros de la operación',
    'Prelectura de estado actual',
    'Procesar prelectura',
    'Poslectura de estado final',
    'Procesar poslectura y construir contrato',
)
INTERNAS = frozenset({
    'Normalizar prelectura', 'Validar marcadores de prelectura',
    'Calcular estado previo', 'Normalizar poslectura',
    'Validar marcadores de poslectura', 'Calcular estado final',
    'Calcular cambio y verificación', 'Construir contrato final de gestión',
})
MENSAJES = (
    'Marcadores de lectura inválidos', 'Salida inesperada antes de la lectura',
    'Bloque de reglas incompleto o inesperado',
    'Propiedad de regla incompleta o duplicada', 'Regla incompleta',
    'Estado de regla ausente o desconocido', 'Regla ambigua, duplicada o incompatible',
    'Falta lectura explícita de disabled', 'Lectura explícita de disabled inválida',
    'Lectura explícita de disabled duplicada o incompleta', 'Lecturas de disabled contradictorias',
    'Reglas duplicadas',
)


def diagnosticar_lineas(lineas):
    """Reproduce el filtro sobre datos de la tarea de normalización sin exponerlos."""
    if not isinstance(lineas, list) or not all(isinstance(item, str) for item in lineas):
        return ['No se dispone de una lectura normalizada válida para reproducir el filtro.']
    class ErrorFiltro(Exception):
        pass

    # El Python del proyecto no necesariamente incluye Ansible: solo se
    # sustituye la clase de excepción durante la carga local del filtro real.
    nombre = 'ansible.errors'
    anterior = sys.modules.get(nombre)
    errores = types.ModuleType(nombre)
    errores.AnsibleFilterError = ErrorFiltro
    sys.modules[nombre] = errores
    try:
        spec = importlib.util.spec_from_file_location(
            'netagent_filtro_diagnostico', RAIZ / 'ansible/filter_plugins/netagent_bloqueo.py')
        if spec is None or spec.loader is None:
            return ['No se pudo cargar el filtro para la reproducción local.']
        filtro = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(filtro)
    finally:
        if anterior is None:
            sys.modules.pop(nombre, None)
        else:
            sys.modules[nombre] = anterior

    try:
        filtro.estado_gestion(lineas, '192.168.10.50')
        return ['La reproducción local del filtro no detectó errores.']
    except ErrorFiltro as exc:
        mensaje = str(exc)
        resultado = [f'Reproducción local del filtro: {mensaje}' if mensaje in MENSAJES
                     else 'La reproducción local del filtro falló con un error no reconocido.']

    if mensaje == 'Estado de regla ausente o desconocido':
        # No mostrar valores ni líneas del equipo; distinguir solo categorías
        # después de la misma normalización que usa la tarea Ansible.
        dentro = False
        valores = []
        for linea in lineas:
            if linea == 'NETAGENT_RULE_BEGIN_V2':
                dentro = True
            elif linea == 'NETAGENT_RULE_END_V2':
                dentro = False
            elif dentro and linea.startswith('NETAGENT_PROP|disabled|'):
                valores.append(linea.split('|', 2)[2])
        if not valores:
            resultado.append('Disabled: propiedad ausente en las líneas normalizadas de la regla.')
        elif any(valor == '' for valor in valores):
            resultado.append('Disabled: propiedad presente con valor vacío.')
        elif any(valor not in filtro.BOOLEANOS for valor in valores):
            resultado.append('Disabled: propiedad presente con valor no reconocido.')
        else:
            resultado.append('Disabled: solo se encontraron valores reconocidos; revisar el formato de las reglas.')

    if mensaje == 'Regla ambigua, duplicada o incompatible':
        reglas = []
        actual = None
        for linea in lineas:
            if linea == 'NETAGENT_RULE_BEGIN_V2':
                actual = {}
            elif linea == 'NETAGENT_RULE_END_V2' and actual is not None:
                reglas.append(actual)
                actual = None
            elif actual is not None and linea.startswith('NETAGENT_PROP|'):
                _, clave, valor = linea.split('|', 2)
                actual[clave] = valor
            elif actual is not None and linea.startswith('NETAGENT_DISABLED|'):
                _, _, valor = linea.split('|', 2)
                actual.setdefault('disabled', valor)
        if len(reglas) > 1:
            resultado.append('Observación: hay reglas duplicadas.')
        elif len(reglas) == 1:
            regla = reglas[0]
            interpretada = filtro._interpretar(regla, ip='192.168.10.50')
            if interpretada['propiedades_inesperadas']:
                resultado.append('Observación: hay propiedades no reconocidas en la regla.')
            if interpretada['propiedades_faltantes']:
                resultado.append('Observación: faltan propiedades obligatorias de la regla.')
            if interpretada['propiedades_anomalas']:
                resultado.append('Observación: hay propiedades opcionales con valores incompatibles.')
            if not interpretada['coincide_objetivo'] or not interpretada['habilitada']:
                resultado.append('Observación: la regla no coincide con el objetivo o está deshabilitada.')
            if regla.get('comment') != 'NETAGENT_LAB_192.168.10.50':
                resultado.append('Observación: la marca de la regla no es la administrada actual.')
            if not regla.get('.id'):
                resultado.append('Observación: falta un identificador de regla válido.')
    return resultado


def preparar_playbook():
    """Copia únicamente las tareas autorizadas, y valida los comandos de lectura."""
    ruta = RAIZ / 'ansible/gestionar_bloqueo.yml'
    documento = yaml.safe_load(ruta.read_text(encoding='utf-8'))
    if not isinstance(documento, list) or len(documento) != 1:
        raise ValueError('El playbook de gestión tiene una estructura inesperada.')
    play = copy.deepcopy(documento[0])
    if play.get('hosts') != 'r1':
        raise ValueError('El playbook no está limitado a R1.')
    tareas = play.get('tasks', [])
    elegidas = []
    for nombre in NOMBRES:
        encontradas = [t for t in tareas if isinstance(t, dict) and t.get('name') == nombre]
        if len(encontradas) != 1:
            raise ValueError('No se encontraron las tareas esperadas de diagnóstico.')
        elegidas.append(encontradas[0])

    # Comprobar la estructura exacta impide reutilizar el diagnóstico si los
    # comandos del playbook cambian: entonces se requiere revisarlos primero.
    actual = ':foreach r in=[/ip firewall filter print as-value where comment="NETAGENT_LAB_{{ ip }}"] do={ :put "NETAGENT_RULE_BEGIN_V2"; :foreach k,v in=$r do={ :put ("NETAGENT_PROP|" . $k . "|" . [:tostr $v]) }; :local id ($r->".id"); :put ("NETAGENT_DISABLED|" . [:tostr $id] . "|" . [:tostr [/ip firewall filter get $id disabled]]); :put "NETAGENT_RULE_END_V2" }'
    antiguo = '{% if ip == "192.168.10.50" %}:foreach r in=[/ip firewall filter print as-value where comment="NetAgent-IA:CU02:PC1-WAN"] do={ :put "NETAGENT_RULE_BEGIN_V2"; :foreach k,v in=$r do={ :put ("NETAGENT_PROP|" . $k . "|" . [:tostr $v]) }; :local id ($r->".id"); :put ("NETAGENT_DISABLED|" . [:tostr $id] . "|" . [:tostr [/ip firewall filter get $id disabled]]); :put "NETAGENT_RULE_END_V2" }{% else %}:put ""{% endif %}'
    esperados = [':put "NETAGENT_BEGIN_V1"', actual, antiguo, ':put "NETAGENT_END_V1"']
    if 'ansible.builtin.assert' not in elegidas[0]:
        raise ValueError('La tarea de validación cambió; hay que revisar el diagnóstico.')
    for tarea in (elegidas[1], elegidas[3]):
        if tarea.get('community.routeros.command', {}).get('commands') != esperados:
            raise ValueError('Los comandos de lectura cambiaron; hay que revisar el diagnóstico.')
    for tarea, fase in ((elegidas[2], 'pre'), (elegidas[4], 'post')):
        if (tarea.get('ansible.builtin.include_tasks') != 'tareas/procesar_gestion_bloqueo.yml'
                or tarea.get('vars', {}).get('netagent_fase') != fase):
            raise ValueError('El procesamiento cambió; hay que revisar el diagnóstico.')
    play['tasks'] = elegidas
    return [play]


def informar(proceso):
    """Expone únicamente nombres conocidos y mensajes previamente aprobados."""
    print(f'Código de salida: {proceso.returncode}')
    try:
        documento = json.loads(proceso.stdout)
        if not isinstance(documento, dict) or not isinstance(documento.get('plays'), list):
            raise ValueError
    except (ValueError, TypeError):
        print('Ansible no devolvió un resultado JSON reconocible; salida cruda ocultada.')
        return False

    fallos = []
    ejecutadas = set()
    lecturas = {}
    lecturas_brutas = {}
    for play in documento['plays']:
        for tarea in play.get('tasks', []):
            nombre = tarea.get('task', {}).get('name', '')
            for resultado in tarea.get('hosts', {}).values():
                if not isinstance(resultado, dict):
                    continue
                if not resultado.get('skipped'):
                    ejecutadas.add(nombre)
                if nombre in {'Normalizar prelectura', 'Normalizar poslectura'}:
                    clave = 'pre_lineas' if nombre == 'Normalizar prelectura' else 'post_lineas'
                    lecturas[clave] = resultado.get('ansible_facts', {}).get(clave)
                if nombre in {'Prelectura de estado actual', 'Poslectura de estado final'}:
                    clave = 'pre_lineas' if nombre == 'Prelectura de estado actual' else 'post_lineas'
                    lecturas_brutas[clave] = resultado.get('stdout_lines')
                if resultado.get('failed') or resultado.get('unreachable'):
                    seguro = nombre if nombre in set(NOMBRES) | INTERNAS else 'Tarea no reconocida'
                    detalle = json.dumps(resultado, ensure_ascii=False)
                    mensajes = [mensaje for mensaje in MENSAJES if mensaje in detalle]
                    fallos.append((seguro, bool(resultado.get('unreachable')), mensajes))

    for nombre, inaccesible, mensajes in fallos:
        print(f'Tarea fallida: {nombre}')
        if inaccesible:
            print('Detalle: equipo no accesible o autenticación fallida.')
        elif mensajes:
            for mensaje in mensajes:
                print(f'Detalle seguro: {mensaje}')
        else:
            print('Detalle oculto por no_log o no reconocido; salida cruda ocultada.')
        clave = {'Calcular estado previo': 'pre_lineas',
                 'Calcular estado final': 'post_lineas'}.get(nombre)
        if clave and clave in lecturas:
            for detalle in diagnosticar_lineas(lecturas[clave]):
                print(detalle)
            bruto = lecturas_brutas.get(clave)
            if isinstance(bruto, list) and all(isinstance(grupo, list) for grupo in bruto):
                presente = any(isinstance(linea, str) and 'NETAGENT_PROP|disabled|' in linea
                               for grupo in bruto for linea in grupo)
                print('Disabled en stdout_lines original: ' +
                      ('marcador completo presente.' if presente else
                       'marcador completo no encontrado en una línea; podría estar ausente o fragmentado.'))

    requeridas = {'Calcular estado previo', 'Calcular estado final',
                  'Construir contrato final de gestión'}
    stats = documento.get('stats', {}).get('r1', {})
    ok = (proceso.returncode == 0 and not fallos and requeridas <= ejecutadas
          and stats.get('failures') == 0 and stats.get('unreachable') == 0)
    if ok:
        print('Estado: prelectura y poslectura procesadas correctamente.')
    elif not fallos:
        print('Estado: diagnóstico incompleto o con fallos; detalle no disponible.')
    else:
        print('Estado: diagnóstico con fallos.')
    print('No se ejecutaron tareas de modificación del firewall.')
    return ok


def main():
    try:
        playbook = preparar_playbook()
        load_dotenv(RAIZ / '.env', override=False)
        with tempfile.TemporaryDirectory(prefix='netagent-cu02-') as directorio:
            temporal = Path(directorio)
            (temporal / 'tareas').mkdir()
            (temporal / 'filter_plugins').mkdir()
            shutil.copy(RAIZ / 'ansible/tareas/procesar_gestion_bloqueo.yml', temporal / 'tareas')
            shutil.copy(RAIZ / 'ansible/filter_plugins/netagent_bloqueo.py', temporal / 'filter_plugins')
            ruta = temporal / 'diagnostico.yml'
            ruta.write_text(yaml.safe_dump(playbook, allow_unicode=True, sort_keys=False), encoding='utf-8')
            entorno = os.environ.copy()
            entorno.update({
                'ANSIBLE_CONFIG': str(RAIZ / 'ansible/ansible.cfg'),
                'ANSIBLE_STDOUT_CALLBACK': 'ansible.posix.json',
                'ANSIBLE_FILTER_PLUGINS': str(temporal / 'filter_plugins'),
                'ANSIBLE_LOCAL_TEMP': str(temporal),
                'ANSIBLE_NOCOLOR': '1',
            })
            comando = [
                'ansible-playbook', '-i', str(RAIZ / 'ansible/inventario.ini'),
                '--limit', 'r1', '--extra-vars',
                json.dumps({'ip': '192.168.10.50', 'accion': 'bloquear'}), str(ruta),
            ]
            proceso = subprocess.run(comando, cwd=RAIZ, env=entorno,
                                     capture_output=True, text=True, timeout=120, check=False)
            return 0 if informar(proceso) else 1
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, subprocess.TimeoutExpired):
        print('No se pudo preparar o completar el diagnóstico; revise el entorno y el playbook.')
        print('No se ejecutaron tareas de modificación del firewall.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
