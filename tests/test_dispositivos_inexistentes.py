"""Pruebas de validación determinista de dispositivos contra el inventario."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


class ValidacionDispositivos(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")
        self.consultas: list[str] = []
        self.escrituras: list[dict] = []
        real = catalogo_por_defecto()

        def consultar(equipo: str, **_opciones):
            self.consultas.append(equipo)
            return {
                "ok": True,
                "equipo": equipo.upper(),
                "interfaces": [
                    {"nombre": "ether1", "estado": "running", "ip": "10.0.0.1/24", "comentario": "demo"}
                ],
                "changed": False,
                "error": None,
            }

        def escribir(**argumentos):
            self.escrituras.append(dict(argumentos))
            return {"ok": True, "changed": True, "error": None}

        self.catalogo = Catalogo(
            [
                Herramienta(real.obtener(CONSULTAR_INTERFACES).definicion, consultar),
                Herramienta(real.obtener(ACTUALIZAR_COMENTARIO).definicion, escribir),
            ]
        )

    def construir(self, ajustes=None, proveedor=None):
        return Cerebro(
            ajustes or self.ajustes,
            proveedor=proveedor,
            catalogo=self.catalogo,
            salida=lambda _texto: None,
            confirmar=lambda _plan: True,
            registro=self.registro,
        )

    def test_analiza_r3_rechaza_deterministicamente_y_muestra_disponibles(self):
        respuesta = self.construir().procesar("Analiza R3")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "dispositivo_no_configurado")
        self.assertIn("R3 no está configurado", respuesta.texto)
        self.assertIn("R1", respuesta.texto)
        self.assertIn("SW1", respuesta.texto)
        self.assertEqual(self.consultas, [])
        self.assertEqual(self.escrituras, [])

    def test_dispositivo_inexistente_no_delega_la_decision_al_modelo(self):
        class ProveedorNoDebeUsarse:
            def responder(self, *_argumentos):
                raise AssertionError("el modelo no debe decidir sobre R3")

        respuesta = self.construir(proveedor=ProveedorNoDebeUsarse()).procesar(
            "Analiza R3"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "dispositivo_no_configurado")
        self.assertEqual(self.consultas, [])

    def test_interfaces_de_r3_rechaza_sin_consultar_al_modelo_ni_al_equipo(self):
        respuesta = self.construir().procesar("Muéstrame las interfaces de R3")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "dispositivo_no_configurado")
        self.assertIn("Dispositivos disponibles", respuesta.texto)
        self.assertEqual(self.consultas, [])
        self.assertEqual(self.escrituras, [])

    def test_consulta_mixta_procesa_r1_y_reporta_r3(self):
        respuesta = self.construir().procesar("Muéstrame las interfaces de R1 y R3")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "consulta_parcial")
        self.assertIn("Interfaces de R1", respuesta.texto)
        self.assertIn("R3 no está configurado", respuesta.texto)
        self.assertEqual(self.consultas, ["r1"])
        self.assertEqual(self.escrituras, [])

    def test_inventario_vacio_rechaza_sin_intentar_conexion(self):
        ajustes = replace(self.ajustes, dispositivos={})
        respuesta = self.construir(ajustes).procesar("Analiza R3")

        self.assertFalse(respuesta.ok)
        self.assertIn("No hay dispositivos configurados en el inventario", respuesta.texto)
        self.assertEqual(self.consultas, [])
        self.assertEqual(self.escrituras, [])

    def test_r1_y_sw1_validos_siguen_funcionando(self):
        r1 = self.construir().procesar("Analiza R1")
        sw1 = self.construir().procesar("Analiza SW1")

        self.assertTrue(r1.ok)
        self.assertTrue(sw1.ok)
        self.assertIn("Análisis de R1", r1.texto)
        self.assertIn("Análisis de SW1", sw1.texto)
        self.assertEqual(self.escrituras, [])


if __name__ == "__main__":
    unittest.main()
