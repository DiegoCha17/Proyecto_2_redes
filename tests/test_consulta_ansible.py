"""Pruebas sin conexión del adaptador de lectura Ansible."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agente.consulta_ansible import consultar_bloqueo
from herramientas.comun import ErrorHerramienta


def documento():
    dato = {'ok': True, 'equipo': 'R1', 'origen': '192.168.10.50', 'destino': '172.16.100.10',
            'bloqueo_configurado': False, 'regla_administrada_existente': False,
            'existe': False, 'reglas': [], 'error': None,
            'reglas_duplicadas': False, 'segura_para_modificar': False,
            'bloqueo_efectivo': None, 'changed': False, 'interno': 'dato no permitido'}
    return {'stats': {'r1': {'failures': 0, 'unreachable': 0}}, 'plays': [{'tasks': [
        {'task': {'name': 'Resultado JSON de NetAgent-IA'},
         'hosts': {'r1': {'netagent_resultado': dato}}}]}]}


class PruebaConsultaAnsible(unittest.TestCase):
    @patch('agente.consulta_ansible.subprocess.run')
    def test_redaccion_de_clave_no_altera_estado_vacio(self, ejecutar):
        doc = documento()
        dato = doc['plays'][0]['tasks'][0]['hosts']['r1']['netagent_resultado']
        dato['regla_$REDACTED$istrada_existente'] = dato.pop('regla_administrada_existente')
        ejecutar.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(doc))
        resultado = consultar_bloqueo('r1')
        self.assertFalse(resultado['regla_administrada_existente'])
        self.assertFalse(resultado['bloqueo_configurado'])
        self.assertFalse(resultado['segura_para_modificar'])

    @patch('agente.consulta_ansible.subprocess.run')
    def test_diagnostico_redactado_llega_a_vista_previa_y_cancelacion(self, ejecutar):
        from agente.catalogo import CONSULTAR_BLOQUEO, GESTIONAR_BLOQUEO, Catalogo, Herramienta, catalogo_por_defecto
        from agente.cerebro import Cerebro, RegistroSesion
        from tests.test_gestionar_bloqueo import ajustes
        import tempfile

        doc = documento()
        dato = doc['plays'][0]['tasks'][0]['hosts']['r1']['netagent_resultado']
        dato['regla_$REDACTED$istrada_existente'] = dato.pop('regla_administrada_existente')
        ejecutar.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(doc))
        real = catalogo_por_defecto()
        escrituras, vistas, aprobaciones = [], [], []
        catalogo = Catalogo([
            Herramienta(real.obtener(CONSULTAR_BLOQUEO).definicion, consultar_bloqueo),
            Herramienta(real.obtener(GESTIONAR_BLOQUEO).definicion, lambda **kw: escrituras.append(kw)),
        ])
        with tempfile.TemporaryDirectory() as carpeta:
            cerebro = Cerebro(ajustes(), catalogo=catalogo, registro=RegistroSesion(carpeta),
                              salida=vistas.append, confirmar=lambda plan: aprobaciones.append(plan) or False)
            respuesta = cerebro.procesar('Bloquea PC1 en R1 hacia el Servidor-WAN.')
        self.assertEqual(respuesta.tipo, 'cancelado')
        self.assertEqual(len(aprobaciones), 1)
        self.assertTrue(any('192.168.10.50' in vista and '172.16.100.10' in vista for vista in vistas))
        self.assertFalse(escrituras)

    @patch('agente.consulta_ansible.subprocess.run')
    def test_lectura_incompleta_o_contradictoria_se_rechaza(self, ejecutar):
        for cambio in ('sin_alias', 'alias_contradictorio', 'reglas_contradictorias', 'sin_reglas', 'error'):
            doc = documento()
            dato = doc['plays'][0]['tasks'][0]['hosts']['r1']['netagent_resultado']
            if cambio == 'sin_alias':
                del dato['existe']
            elif cambio == 'alias_contradictorio':
                dato['existe'] = True
            elif cambio == 'reglas_contradictorias':
                dato['reglas'] = [{}]
            elif cambio == 'sin_reglas':
                del dato['reglas']
            else:
                dato['error'] = {'mensaje': 'fallo'}
            ejecutar.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(doc))
            with self.subTest(cambio=cambio), self.assertRaises(ErrorHerramienta):
                consultar_bloqueo('r1')

    @patch('agente.consulta_ansible.subprocess.run')
    def test_lectura_sin_bloqueo_filtra_y_no_usa_shell(self, ejecutar):
        ejecutar.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(documento()))
        resultado = consultar_bloqueo('r1')
        self.assertFalse(resultado['bloqueo_configurado'])
        self.assertIsNone(resultado['bloqueo_efectivo'])
        self.assertNotIn('interno', resultado)
        args, kwargs = ejecutar.call_args
        self.assertEqual(args[0][0], 'ansible-playbook')
        self.assertNotIn('gestionar_bloqueo', str(args))
        self.assertNotIn('shell', kwargs)

    @patch('agente.consulta_ansible.subprocess.run')
    def test_fallos_y_salida_invalida_no_exponen_detalles(self, ejecutar):
        for respuesta in (SimpleNamespace(returncode=1, stdout='secreto'),
                          SimpleNamespace(returncode=0, stdout='secreto'),
                          SimpleNamespace(returncode=0, stdout=json.dumps({'stats': []}))):
            ejecutar.return_value = respuesta
            with self.assertRaises(ErrorHerramienta) as error:
                consultar_bloqueo('r1')
            self.assertNotIn('secreto', str(error.exception))
        ejecutar.side_effect = subprocess.TimeoutExpired('ansible-playbook', 120)
        with self.assertRaises(ErrorHerramienta):
            consultar_bloqueo('r1')

    @patch('agente.consulta_ansible.subprocess.run')
    def test_equipo_no_permitido_no_ejecuta_ansible(self, ejecutar):
        with self.assertRaises(ErrorHerramienta):
            consultar_bloqueo('sw1')
        ejecutar.assert_not_called()


if __name__ == '__main__':
    unittest.main()
