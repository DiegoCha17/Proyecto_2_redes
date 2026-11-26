"""Ciclo CU-02 con catálogo y proveedor simulados; sin red."""
from dataclasses import replace
from agente.catalogo import CONSULTAR_BLOQUEO, GESTIONAR_BLOQUEO, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import LlamadaHerramienta, RespuestaModelo
from tests.test_gestionar_bloqueo import ajustes

SOLICITUD = 'Bloquea 192.168.10.50 en R1.'
ARGS = {'equipo': 'r1', 'ip': '192.168.10.50', 'accion': 'bloquear'}


def construir(tmp_path, lecturas, *, confirmar=True, llamada_modelo=True):
    real = catalogo_por_defecto()
    historial, vistas, preguntas = [], [], []
    estados = iter(lecturas)
    def leer(equipo, ip, ajustes):
        historial.append('lectura')
        estado = next(estados)
        if estado is None:
            return {'ok': False, 'error': {'tipo': 'consulta', 'mensaje': 'falló'}}
        return {'ok': True, 'origen': ip, 'bloqueo_configurado': estado,
                'regla_administrada_existente': estado, 'segura_para_modificar': estado,
                'reglas_duplicadas': False, 'error': None}
    def escribir(**kwargs):
        historial.append('escritura')
        return {'ok': True, 'equipo': 'R1', 'ip': kwargs['ip'], 'bloqueada': True,
                'changed': True, 'verified': True, 'error': None}
    class Proveedor:
        def responder_historial(self, mensajes, herramientas):
            historial.append('modelo')
            if not llamada_modelo:
                return RespuestaModelo(texto='No propongo herramienta')
            return RespuestaModelo(llamadas=(LlamadaHerramienta(GESTIONAR_BLOQUEO, ARGS, 'seleccion'),))
    catalogo = Catalogo([Herramienta(real.obtener(CONSULTAR_BLOQUEO).definicion, leer),
                         Herramienta(real.obtener(GESTIONAR_BLOQUEO).definicion, escribir)])
    registro = RegistroSesion(tmp_path)
    cerebro = Cerebro(ajustes(), proveedor=Proveedor(), catalogo=catalogo, registro=registro,
                      salida=vistas.append, confirmar=lambda plan: preguntas.append(plan) or confirmar)
    return cerebro, historial, vistas, preguntas, registro


def test_ciclo_y_registro_con_function_calling(tmp_path):
    cerebro, historial, vistas, preguntas, registro = construir(tmp_path, [False, True])
    respuesta = cerebro.procesar(SOLICITUD)
    assert respuesta.ok and historial == ['lectura', 'modelo', 'escritura', 'lectura']
    assert len(preguntas) == 1
    assert any('gestionar_bloqueo' in v and '192.168.10.50' in v for v in vistas)
    notas = [e.get('nota', '') for e in registro.eventos]
    for texto in ('diagnóstico previo', 'selección mediante function calling', 'propuesta',
                  'aprobación explícita', 'ejecución de escritura', 'verificación independiente'):
        assert any(texto in nota for nota in notas)
    assert any(e.get('plan', {}).get('accion', {}).get('herramienta') == CONSULTAR_BLOQUEO
               and e.get('resultado', {}).get('salida', {}).get('origen') == ARGS['ip'] for e in registro.eventos)


def test_fallo_diagnostico_no_propone_ni_escribe(tmp_path):
    cerebro, historial, _, preguntas, _ = construir(tmp_path, [None])
    respuesta = cerebro.procesar(SOLICITUD)
    assert not respuesta.ok and historial == ['lectura'] and not preguntas


def test_verificacion_independiente_contradictoria_no_informa_exito(tmp_path):
    cerebro, historial, _, _, _ = construir(tmp_path, [False, False])
    respuesta = cerebro.procesar(SOLICITUD)
    assert not respuesta.ok and respuesta.error['tipo'] == 'verificacion_fallida'
    assert historial == ['lectura', 'modelo', 'escritura', 'lectura']


def test_ya_bloqueada_no_solicita_aprobacion_ni_escribe(tmp_path):
    cerebro, historial, _, preguntas, _ = construir(tmp_path, [True])
    respuesta = cerebro.procesar(SOLICITUD)
    assert respuesta.ok and not preguntas and historial == ['lectura']


def test_orden_por_nombre_diagnostica_y_permanece_cancelada(tmp_path):
    cerebro, historial, vistas, preguntas, _ = construir(tmp_path, [False], confirmar=False)
    respuesta = cerebro.procesar('Bloquea PC1 en R1 hacia el Servidor-WAN.')
    assert respuesta.tipo == 'cancelado'
    assert historial == ['lectura', 'modelo']
    assert preguntas[0].accion.argumentos == ARGS
    assert any('gestionar_bloqueo' in v and '192.168.10.50' in v and '172.16.100.10' in v for v in vistas)
    assert any('Diagnóstico' in v for v in vistas)


def test_propuesta_inventada_del_modelo_no_sustituye_inventario(tmp_path):
    cerebro, historial, _, preguntas, registro = construir(tmp_path, [False], confirmar=False)
    def proponer(*args):
        historial.append('modelo')
        return RespuestaModelo(llamadas=(LlamadaHerramienta(GESTIONAR_BLOQUEO,
            {'equipo': 'r1', 'ip': '192.168.1.10', 'accion': 'bloquear'}, 'invencion'),))
    cerebro._proveedor.responder_historial = proponer
    respuesta = cerebro.procesar('Bloquea PC1 en R1 hacia el Servidor-WAN.')
    assert respuesta.tipo == 'cancelado' and historial == ['lectura', 'modelo']
    assert preguntas[0].accion.argumentos == ARGS
    assert any('propuesta del modelo no coincide' in e.get('nota', '') for e in registro.eventos)


def test_orden_ambigua_y_propuesta_inventada_no_llegan_a_politicas(tmp_path):
    cerebro, historial, _, preguntas, _ = construir(tmp_path, [])
    def proponer(*args):
        historial.append('modelo')
        return RespuestaModelo(llamadas=(LlamadaHerramienta(GESTIONAR_BLOQUEO,
            {'equipo': 'r1', 'ip': '192.168.1.10', 'accion': 'bloquear'}, 'invencion'),))
    cerebro._proveedor.responder_historial = proponer
    respuesta = cerebro.procesar('Quizá bloquea PC1 en R1 hacia el Servidor-WAN.')
    assert not respuesta.ok and respuesta.error['tipo'] == 'solicitud_ambigua'
    assert historial == ['modelo'] and not preguntas


def test_consulta_modelo_con_ip_inventada_no_lee_otro_equipo(tmp_path):
    cerebro, historial, _, preguntas, _ = construir(tmp_path, [])
    def proponer(*args):
        historial.append('modelo')
        return RespuestaModelo(llamadas=(LlamadaHerramienta(CONSULTAR_BLOQUEO,
            {'equipo': 'r1', 'ip': '192.168.1.10'}, 'invencion'),))
    cerebro._proveedor.responder_historial = proponer
    respuesta = cerebro.procesar('Consulta el bloqueo de PC1 en R1.')
    assert not respuesta.ok and respuesta.error['tipo'] == 'solicitud_ambigua'
    assert historial == ['modelo'] and not preguntas


def test_destino_inventario_incompatible_detiene_ciclo(tmp_path):
    cerebro, historial, _, preguntas, _ = construir(tmp_path, [])
    laboratorio = dict(cerebro.ajustes.laboratorio, servidor_wan='172.16.100.11')
    cerebro.ajustes = replace(cerebro.ajustes, laboratorio=laboratorio)
    respuesta = cerebro.procesar('Bloquea PC1 en R1 hacia el Servidor-WAN.')
    assert not respuesta.ok and respuesta.error['tipo'] == 'solicitud_ambigua'
    assert not historial and not preguntas


def test_respaldo_del_modelo_se_registra_sin_fingir_function_calling(tmp_path):
    cerebro, _, _, _, registro = construir(tmp_path, [False, True], llamada_modelo=False)
    assert cerebro.procesar(SOLICITUD).ok
    assert any('respaldo determinista' in e.get('nota', '') for e in registro.eventos)
    assert not any(e.get('nota') == 'selección mediante function calling' for e in registro.eventos)


def test_regla_existente_deshabilitada_se_rechaza_antes_de_aprobar(tmp_path):
    real = catalogo_por_defecto()
    preguntas, escrituras = [], []
    def leer(**kwargs):
        return {'ok': True, 'origen': kwargs['ip'], 'bloqueo_configurado': False,
                'regla_administrada_existente': True, 'segura_para_modificar': True,
                'reglas_duplicadas': False, 'error': None}
    catalogo = Catalogo([Herramienta(real.obtener(CONSULTAR_BLOQUEO).definicion, leer),
                         Herramienta(real.obtener(GESTIONAR_BLOQUEO).definicion,
                                     lambda **kwargs: escrituras.append(kwargs))])
    cerebro = Cerebro(ajustes(), catalogo=catalogo, registro=RegistroSesion(tmp_path),
                      salida=lambda _: None, confirmar=lambda plan: preguntas.append(plan) or True)
    resultado = cerebro.procesar('Permite 192.168.10.50 en R1.')
    assert not resultado.ok and resultado.error['tipo'] == 'diagnostico_fallido'
    assert not preguntas and not escrituras
