"""Diagnóstico local de CU-02 con datos simulados, sin acceso a equipos."""
import json
from types import SimpleNamespace

from scripts.diagnosticar_cu02 import diagnosticar_lineas, informar, preparar_playbook
from tests.test_cu02_correcciones import lectura, regla


def test_reproduce_filtro_y_detecta_propiedades_desconocidas():
    lineas = lectura(regla(**{'propiedad-privada': 'secreto-prueba'}))
    resultado = diagnosticar_lineas(lineas)
    assert 'Reproducción local del filtro: Regla ambigua, duplicada o incompatible' in resultado
    assert 'Observación: hay propiedades no reconocidas en la regla.' in resultado
    assert 'secreto-prueba' not in str(resultado)
    assert 'propiedad-privada' not in str(resultado)


def test_clasifica_disabled_sin_exponer_valores():
    sin_disabled = [linea for linea in regla() if not linea.startswith('NETAGENT_PROP|disabled|')]
    assert 'La reproducción local del filtro no detectó errores.' in diagnosticar_lineas(lectura(sin_disabled))
    incompleta = [linea for linea in sin_disabled if not linea.startswith('NETAGENT_DISABLED|')]
    assert 'Reproducción local del filtro: Falta lectura explícita de disabled' in diagnosticar_lineas(lectura(incompleta))
    resultado = diagnosticar_lineas(lectura(regla(disabled='valor-privado')))
    assert 'Reproducción local del filtro: Lectura explícita de disabled inválida' in resultado
    assert 'valor-privado' not in str(resultado)


def test_no_interpreta_fallo_protegido_como_confirmado_si_replay_pasa(capsys):
    lineas = lectura([])
    documento = {'plays': [{'tasks': [
        {'task': {'name': 'Normalizar prelectura'},
         'hosts': {'r1': {'ansible_facts': {'pre_lineas': lineas}}}},
        {'task': {'name': 'Calcular estado previo'},
         'hosts': {'r1': {'failed': True, 'censored': 'secreto-prueba'}}},
    ]}], 'stats': {'r1': {'failures': 1, 'unreachable': 0}}}
    assert not informar(SimpleNamespace(returncode=2, stdout=json.dumps(documento)))
    salida = capsys.readouterr().out
    assert 'Tarea fallida: Calcular estado previo' in salida
    assert 'La reproducción local del filtro no detectó errores.' in salida
    assert 'secreto-prueba' not in salida


def test_playbook_diagnostico_no_incluye_escrituras():
    tareas = preparar_playbook()[0]['tasks']
    assert len(tareas) == 5
    assert all('Aplicar bloqueo' not in tarea['name'] and 'Quitar bloqueo' not in tarea['name'] for tarea in tareas)
