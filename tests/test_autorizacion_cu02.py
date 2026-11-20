import unittest
from dataclasses import replace

from agente.autorizacion_cu02 import preparar_propuesta, solicitar_aprobacion, autorizada


class PruebaAutorizacionCU02(unittest.TestCase):
    def setUp(self):
        self.argumentos = {'operacion': 'bloquear', 'equipo': 'r1',
                           'origen': '192.168.10.50', 'destino': '172.16.100.10'}
        self.propuesta = preparar_propuesta(self.argumentos)

    def test_aprobacion_explicita_cubre_exactamente_propuesta(self):
        marca = solicitar_aprobacion(self.propuesta, lambda p: True)
        self.assertTrue(autorizada(self.propuesta, marca))

    def test_aprobacion_ausente_o_no_explicita(self):
        for respuesta in (False, None, 'sí', 1):
            self.assertIsNone(solicitar_aprobacion(self.propuesta, lambda p: respuesta))
        self.assertFalse(autorizada(self.propuesta, None))
        self.assertFalse(autorizada(self.propuesta, True))

    def test_no_cubre_parametros_ni_herramienta_distintos(self):
        marca = solicitar_aprobacion(self.propuesta, lambda p: True)
        for cambio in ({'operacion': 'desbloquear'}, {'equipo': 'sw1'},
                       {'origen': '192.168.10.60'}, {'destino': '172.16.100.11'},
                       {'herramienta': 'otra'}):
            self.assertFalse(autorizada(replace(self.propuesta, **cambio), marca))

    def test_parametros_no_autorizados_o_extras(self):
        for cambio in ({'operacion': 'otra'}, {'equipo': 'sw1'}, {'origen': '0.0.0.0'},
                       {'destino': '0.0.0.0'}, {'confirmacion': True}):
            with self.assertRaises(ValueError):
                preparar_propuesta({**self.argumentos, **cambio})


if __name__ == '__main__':
    unittest.main()
