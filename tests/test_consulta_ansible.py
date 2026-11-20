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
            'reglas_duplicadas': False, 'segura_para_modificar': False,
            'bloqueo_efectivo': None, 'changed': False, 'interno': 'dato no permitido'}
    return {'stats': {'r1': {'failures': 0, 'unreachable': 0}}, 'plays': [{'tasks': [
        {'task': {'name': 'Resultado JSON de NetAgent-IA'},
         'hosts': {'r1': {'netagent_resultado': dato}}}]}]}


class PruebaConsultaAnsible(unittest.TestCase):
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
