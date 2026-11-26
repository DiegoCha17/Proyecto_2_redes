"""Pruebas locales de CU-02; no establecen conexiones de red."""
import pytest
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

class AnsibleFilterError(Exception):
    pass

errores = types.ModuleType("ansible.errors")
errores.AnsibleFilterError = AnsibleFilterError
with patch.dict(sys.modules, {"ansible.errors": errores}):
    spec = importlib.util.spec_from_file_location("netagent_filtro_test", Path(__file__).resolve().parents[1] / "ansible/filter_plugins/netagent_bloqueo.py")
    filtro = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(filtro)
procesar_bloqueo, estado_gestion = filtro.procesar_bloqueo, filtro.estado_gestion
from agente.interpretar import extraer_solicitud_bloqueo


def regla(ip='192.168.10.50', marca=None, **cambios):
    propiedades = {'.id': '*A', 'chain': 'forward', 'action': 'drop',
                   'src-address': ip, 'dst-address': '172.16.100.10',
                   'disabled': 'false', 'comment': marca or 'NETAGENT_LAB_' + ip}
    propiedades.update(cambios)
    return ['NETAGENT_RULE_BEGIN_V2',
            *[f'NETAGENT_PROP|{k}|{v}' for k, v in propiedades.items()],
            f'NETAGENT_DISABLED|{propiedades[".id"]}|{propiedades["disabled"]}', 'NETAGENT_RULE_END_V2']


def lectura(reglas):
    return ['NETAGENT_BEGIN_V1', *reglas, 'NETAGENT_END_V1']


ECO_FRAGMENTADO = [
    ':foreach r in=[/ip firewall filter print as-value where c',
    '< firewall filter print as-value where co',
    '<omment="NETAGENT_LAB_192.168.10.50"] do=',
    '<={ :put "NETAGENT_RULE_BEGIN_V2"; :foreach k,v in=$r do={ :put ("NETAGENT_PROP',
]


def test_prelectura_vacia_con_ecos_fragmentados():
    assert estado_gestion(lectura(ECO_FRAGMENTADO), '192.168.10.50') is False


def test_prelectura_con_stdout_real_de_routeros():
    import json
    salida = json.loads((Path(__file__).parent / 'fixtures/cu02_stdout_lines_r1.json').read_text())
    lineas = [linea.strip() for comando in salida for linea in comando if linea.strip()]
    assert estado_gestion(lineas, '192.168.10.50') is False
    con_eco = lineas[1:-1]
    assert estado_gestion(lectura(con_eco + regla()), '192.168.10.50') is True
    for contenido in (regla() + regla(), regla(action='accept'),
                      regla(marca='NetAgent-IA:CU02:PC1-WAN'),
                      ['<texto desconocido>'],
                      regla()[:2] + ['< firewall filter print as-value where co>'] + regla()[2:]):
        with pytest.raises(AnsibleFilterError):
            estado_gestion(lectura(con_eco + contenido), '192.168.10.50')


def test_ecos_no_ocultan_reglas_ni_errores():
    ip = '192.168.10.50'
    assert estado_gestion(lectura(ECO_FRAGMENTADO + regla()), ip) is True
    for contenido in (
        ECO_FRAGMENTADO + regla() + regla(),
        ECO_FRAGMENTADO + regla(action='accept'),
        ECO_FRAGMENTADO + regla(marca='NetAgent-IA:CU02:PC1-WAN'),
        ECO_FRAGMENTADO + ['error inesperado'],
        ECO_FRAGMENTADO + ['<texto desconocido'],
        regla()[:2] + ECO_FRAGMENTADO + regla()[2:],
        regla()[:2] + ['< firewall filter print as-value where co'] + regla()[2:],
    ):
        with pytest.raises(AnsibleFilterError):
            estado_gestion(lectura(contenido), ip)


def test_consulta_reconoce_marca_actual_y_origen_variable():
    ip = '192.168.10.60'
    resultado = procesar_bloqueo(regla(ip), ip)
    assert resultado['origen'] == ip
    assert resultado['bloqueo_configurado'] and resultado['segura_para_modificar']
    assert resultado['bloqueo_efectivo'] is None and resultado['changed'] is False


def test_estado_gestion_idempotente_y_rechaza_ambiguas():
    ip = '192.168.10.50'
    assert estado_gestion(lectura([]), ip) is False
    assert estado_gestion(lectura(regla()), ip) is True
    for bloque in (regla() + regla(), regla(action='accept'), regla(disabled='true'),
                   regla(protocol='tcp'), regla(marca='NetAgent-IA:CU02:PC1-WAN'),
                   regla()[:-1], regla() + ['error']):
        with pytest.raises(AnsibleFilterError):
            estado_gestion(lectura(bloque), ip)


def test_get_disabled_explicito_con_formato_routeros_y_fallos():
    ip = '192.168.10.50'
    sin_propiedad = [linea for linea in regla() if not linea.startswith('NETAGENT_PROP|disabled|')]
    assert estado_gestion(lectura(sin_propiedad), ip) is True
    assert procesar_bloqueo(sin_propiedad, ip, True)['bloqueo_configurado'] is True
    deshabilitada = [linea.replace('NETAGENT_DISABLED|*A|false', 'NETAGENT_DISABLED|*A|true') for linea in sin_propiedad]
    assert procesar_bloqueo(deshabilitada, ip, True)['bloqueo_configurado'] is False
    for bloque in (
        [l for l in sin_propiedad if not l.startswith('NETAGENT_DISABLED|')],
        [l.replace('NETAGENT_DISABLED|*A|false', 'NETAGENT_DISABLED|*B|false') for l in sin_propiedad],
        [l.replace('NETAGENT_DISABLED|*A|false', 'NETAGENT_DISABLED|*A|') for l in sin_propiedad],
        sin_propiedad[:-1] + ['NETAGENT_DISABLED|*A|false', sin_propiedad[-1]],
        regla()[:-2] + ['NETAGENT_DISABLED|*A|true', 'NETAGENT_RULE_END_V2'],
        deshabilitada,
    ):
        with pytest.raises(AnsibleFilterError):
            estado_gestion(lectura(bloque), ip)


@pytest.mark.parametrize(('texto', 'accion'), [
    ('Bloquea el acceso de PC1 (192.168.10.50) al Servidor-WAN (172.16.100.10) en R1.', 'bloquear'),
    ('Desbloquea el acceso de PC1 (192.168.10.50) al Servidor-WAN (172.16.100.10) en R1.', 'permitir'),
    ('Permite el acceso de PC1 (192.168.10.50) al Servidor-WAN (172.16.100.10) en R1.', 'permitir'),
    ('Usa gestionar_bloqueo con equipo r1, ip 192.168.10.50 y accion bloquear. Solicita mi aprobación antes de ejecutar.', 'bloquear'),
    ('Desbloquea PC1 (192.168.10.50) para que vuelva a acceder al Servidor-WAN (172.16.100.10) en R1.', 'permitir'),
    ('Permite nuevamente el acceso de PC1 (192.168.10.50) al Servidor-WAN (172.16.100.10) en R1.', 'permitir'),
    ('Quita el bloqueo de PC1 (192.168.10.50) hacia el Servidor-WAN en R1.', 'permitir'),
])
def test_ordenes_explicitas(texto, accion):
    assert extraer_solicitud_bloqueo(texto, ip_pc1='192.168.10.50') == {'equipo': 'r1', 'ip': '192.168.10.50', 'accion': accion}


@pytest.mark.parametrize('texto', [
    '¿Bloquea PC1 (192.168.10.50) en R1?',
    'No bloquees 192.168.10.50 en R1.',
    'Consulta si existe bloqueo de 192.168.10.50 en R1.',
    'Bloquea 192.168.10.50 en R1 si está activa.',
    'Bloquea el acceso de PC1 (192.168.10.50) al Servidor-WAN (172.16.100.11) en R1.',
    'Bloquea PC1 en R1.',
    'No desbloquees PC1 (192.168.10.50) en R1.',
    'Desbloquea PC1 (192.168.10.50) en R1 si ya fue aprobado.',
    '¿Puedes desbloquear PC1 (192.168.10.50) en R1?',
    'Quita el bloqueo de PC1 (192.168.10.60) en R1.',
    'Desbloquea PC1 (192.168.10.50 en R1.',
    'Desbloquea PC1 (192.168.10.50) para que acceda a 172.16.100.11 en R1.',
])
def test_no_interpreta_consultas_negaciones_o_condiciones_como_escrituras(texto):
    assert extraer_solicitud_bloqueo(texto) is None
    assert extraer_solicitud_bloqueo(texto, ip_pc1='192.168.10.50') is None


@pytest.mark.parametrize('texto', [
    'Bloquea PC1 en R1 hacia el Servidor-WAN.',
    'Bloquea PC1 hacia el Servidor-WAN en R1.',
    'Bloquea el acceso de PC1 en R1 hacia el Servidor-WAN (172.16.100.10).',
])
def test_bloqueo_alias_resuelve_exclusivamente_inventario(texto):
    assert extraer_solicitud_bloqueo(texto, ip_pc1='192.168.10.77') == {
        'equipo': 'r1', 'ip': '192.168.10.77', 'accion': 'bloquear'}
    assert extraer_solicitud_bloqueo(texto) is None


@pytest.mark.parametrize('texto', [
    'No bloquees PC1 en R1 hacia el Servidor-WAN.',
    'Bloquea PC1 en R1 hacia el Servidor-WAN si está activa.',
    '¿Bloquea PC1 en R1 hacia el Servidor-WAN?',
    'Bloquea PC1 en R1 hacia el Servidor-WAN (172.16.100.11).',
    'Bloquea PC1 en SW1 hacia el Servidor-WAN.',
    'Bloquea y desbloquea PC1 en R1 hacia el Servidor-WAN.',
])
def test_bloqueo_alias_ambiguo_no_se_reconoce(texto):
    assert extraer_solicitud_bloqueo(texto, ip_pc1='192.168.10.50') is None


@pytest.mark.parametrize('texto', [
    'Desbloquea PC1 en R1.',
    'Permite nuevamente el acceso de PC1 al Servidor-WAN en R1.',
    'Quita el bloqueo de PC1 en R1.',
    'Desbloquea PC1.',
    'Permite nuevamente el acceso de PC1.',
    'Quita el bloqueo de PC1.',
])
def test_desbloqueo_alias_resuelve_ip_del_inventario(texto):
    assert extraer_solicitud_bloqueo(texto, ip_pc1='192.168.10.50') == {
        'equipo': 'r1', 'ip': '192.168.10.50', 'accion': 'permitir'}
    assert extraer_solicitud_bloqueo(texto) is None


def test_desbloqueo_natural_diagnostica_y_consulta_modelo_antes_de_aprobacion(tmp_path):
    from agente.cerebro import Cerebro, RegistroSesion
    from tests.test_gestionar_bloqueo import ajustes, FuncionGestion, catalogo_con
    funcion = FuncionGestion()
    preguntas, vistas = [], []
    from agente.interpretar import LlamadaHerramienta, RespuestaModelo
    peticiones = []
    class Proveedor:
        def responder_historial(self, *args):
            peticiones.append(args)
            return RespuestaModelo(llamadas=(LlamadaHerramienta('gestionar_bloqueo',
                {'equipo': 'r1', 'ip': '192.168.10.50', 'accion': 'permitir'}, 'sel'),))
    cerebro = Cerebro(ajustes(), proveedor=Proveedor(), catalogo=catalogo_con(funcion, bloqueada=True),
                      confirmar=lambda plan: preguntas.append(plan) or False,
                      salida=vistas.append, registro=RegistroSesion(tmp_path))
    resultado = cerebro.procesar('Desbloquea PC1 (192.168.10.50) para que vuelva a acceder al Servidor-WAN (172.16.100.10) en R1.')
    assert resultado.tipo == 'cancelado'
    assert len(preguntas) == 1 and not funcion.llamadas
    assert len(peticiones) == 1
    assert preguntas[0].accion.argumentos['accion'] == 'permitir'
    assert any('192.168.10.50' in vista for vista in vistas)
    assert any('172.16.100.10' in vista for vista in vistas)


def test_desbloqueo_alias_aprobado_pasa_politica_y_verificacion(tmp_path):
    from agente.cerebro import Cerebro, RegistroSesion
    from tests.test_gestionar_bloqueo import ajustes, catalogo_con
    llamadas = []
    def gestion(**kwargs):
        llamadas.append(kwargs)
        return {'ok': True, 'equipo': 'R1', 'ip': kwargs['ip'], 'bloqueada': False,
                'changed': True, 'verified': True, 'error': None}
    registro = RegistroSesion(tmp_path)
    cerebro = Cerebro(ajustes(), catalogo=catalogo_con(gestion, bloqueada=True), confirmar=lambda _: True,
                      salida=lambda _: None, registro=registro)
    resultado = cerebro.procesar('Quita el bloqueo de PC1 en R1.')
    assert resultado.ok and resultado.tipo == 'modificacion'
    assert len(llamadas) == 1 and llamadas[0]['accion'] == 'permitir'
    assert llamadas[0]['ip'] == '192.168.10.50' and llamadas[0]['confirmacion'] is True
    assert any('verificación posterior' in e.get('nota', '') for e in registro.eventos)


def test_cerebro_orden_natural_solicita_aprobacion_sin_llamar_modelo(tmp_path):
    from agente.cerebro import Cerebro, RegistroSesion
    from tests.test_gestionar_bloqueo import ajustes, FuncionGestion, catalogo_con
    funcion = FuncionGestion()
    preguntas = []
    cerebro = Cerebro(ajustes(), catalogo=catalogo_con(funcion),
                      confirmar=lambda plan: preguntas.append(plan) or False,
                      salida=lambda _: None, registro=RegistroSesion(tmp_path))
    respuesta = cerebro.procesar('Bloquea 192.168.10.60 en R1')
    assert respuesta.tipo == 'cancelado'
    assert len(preguntas) == 1 and not funcion.llamadas
    assert any('aprobación rechazada' in e.get('nota', '') for e in cerebro._registro.eventos)


def test_propuesta_modelo_no_convierte_consulta_en_escritura(tmp_path):
    from agente.cerebro import Cerebro, RegistroSesion
    from agente.interpretar import Intencion, TIPO_HERRAMIENTA
    from agente.catalogo import GESTIONAR_BLOQUEO
    from tests.test_gestionar_bloqueo import ajustes, FuncionGestion, catalogo_con, ARGUMENTOS
    funcion = FuncionGestion()
    preguntas = []
    cerebro = Cerebro(ajustes(), catalogo=catalogo_con(funcion),
                      confirmar=lambda plan: preguntas.append(plan) or True,
                      salida=lambda _: None, registro=RegistroSesion(tmp_path))
    respuesta = cerebro._atender_herramienta('¿Existe bloqueo para 192.168.10.60 en R1?',
                   Intencion(TIPO_HERRAMIENTA, GESTIONAR_BLOQUEO, ARGUMENTOS))
    assert respuesta.error['tipo'] == 'solicitud_ambigua'
    assert not preguntas and not funcion.llamadas


def test_prelectura_ansible_real_rechaza_duplicados_sin_red(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    if not shutil.which('ansible-playbook'):
        pytest.skip('Ansible no está instalado')
    raiz = Path(__file__).resolve().parents[1]
    entorno = os.environ.copy()
    entorno.update({'ANSIBLE_CONFIG': str(raiz / 'ansible/ansible.cfg'),
                    'ANSIBLE_LOCAL_TEMP': str(tmp_path),
                    'ANSIBLE_FILTER_PLUGINS': str(raiz / 'ansible/filter_plugins'),
                    'ANSIBLE_NOCOLOR': '1'})
    def ejecutar(reglas):
        variables = {'salida_simulada': '\n'.join(lectura(reglas)), 'ip': '192.168.10.50'}
        return subprocess.run(['ansible-playbook', '-i', 'localhost,',
            str(raiz / 'tests/ansible/gestion_bloqueo_local.yml'), '-e', json.dumps(variables)],
            cwd=raiz, env=entorno, capture_output=True, text=True, timeout=45)
    proceso = ejecutar(regla())
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    salida_routeros = [linea for linea in regla() if not linea.startswith('NETAGENT_PROP|disabled|')]
    proceso = ejecutar(salida_routeros)
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    assert ejecutar([linea for linea in salida_routeros if not linea.startswith('NETAGENT_DISABLED|')]).returncode != 0
    assert ejecutar(regla() + regla()).returncode != 0
    variables = {'salida_simulada': '', 'ip': '192.168.10.50',
                 'stdout_simulado': [['NETAGENT_BEGIN_V1'], ECO_FRAGMENTADO,
                                    [], ['NETAGENT_END_V1']]}
    proceso = subprocess.run(['ansible-playbook', '-i', 'localhost,',
        str(raiz / 'tests/ansible/gestion_bloqueo_local.yml'), '-e', json.dumps(variables)],
        cwd=raiz, env=entorno, capture_output=True, text=True, timeout=45)
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    assert 'bloqueada_antes' in proceso.stdout and 'false' in proceso.stdout.lower()
    variables['stdout_simulado'] = json.loads(
        (raiz / 'tests/fixtures/cu02_stdout_lines_r1.json').read_text(encoding='utf-8'))
    proceso = subprocess.run(['ansible-playbook', '-i', 'localhost,',
        str(raiz / 'tests/ansible/gestion_bloqueo_local.yml'), '-e', json.dumps(variables)],
        cwd=raiz, env=entorno, capture_output=True, text=True, timeout=45)
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    assert 'bloqueada_antes' in proceso.stdout and 'false' in proceso.stdout.lower()
