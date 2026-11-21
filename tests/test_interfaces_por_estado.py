"""Pruebas de consultas de interfaces filtradas por estado, sin routers reales."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import extraer_filtro_interfaces_estado
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


INTERFACES = {
    "r1": [
        {"nombre": "ether1", "estado": "running", "ip": "", "comentario": "WAN"},
        {"nombre": "ether2", "estado": "down", "ip": "", "comentario": ""},
        {"nombre": "ether3", "estado": "disabled", "ip": "", "comentario": ""},
    ],
    "sw1": [
        {"nombre": "ether1", "estado": "running", "ip": "", "comentario": "uplink"},
        {"nombre": "ether2", "estado": "down", "ip": "", "comentario": ""},
    ],
}


class ConsultaPorEstado(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")
        self.llamadas: list[str] = []

    def construir(self, datos=None, errores=None):
        datos = datos or INTERFACES
        errores = errores or {}
        real = catalogo_por_defecto().obtener(CONSULTAR_INTERFACES)

        def consultar_falsa(equipo, **_opciones):
            self.llamadas.append(equipo)
            if equipo in errores:
                return {
                    "ok": False,
                    "equipo": equipo.upper(),
                    "interfaces": [],
                    "changed": False,
                    "error": errores[equipo],
                }
            return {
                "ok": True,
                "equipo": equipo.upper(),
                "interfaces": [dict(interfaz) for interfaz in datos[equipo]],
                "changed": False,
                "error": None,
            }

        catalogo = Catalogo(
            [Herramienta(definicion=real.definicion, funcion=consultar_falsa)]
        )
        return Cerebro(
            self.ajustes,
            proveedor=object(),
            catalogo=catalogo,
            salida=lambda texto: None,
            registro=self.registro,
        )

    def test_down_de_r1(self):
        respuesta = self.construir().procesar("¿Qué interfaces de R1 están en estado down?")

        self.assertTrue(respuesta.ok)
        self.assertIn("ether2", respuesta.texto)
        self.assertNotIn("ether1", respuesta.texto)
        self.assertNotIn("ether3", respuesta.texto)
        self.assertEqual(self.llamadas, ["r1"])

    def test_down_de_sw1(self):
        respuesta = self.construir().procesar("Muéstrame las interfaces desconectadas de SW1")

        self.assertTrue(respuesta.ok)
        self.assertIn("ether2", respuesta.texto)
        self.assertNotIn("ether1", respuesta.texto)
        self.assertEqual(self.llamadas, ["sw1"])

    def test_running_de_r1_y_sw1(self):
        respuesta = self.construir().procesar(
            "¿Qué interfaces conectadas están en R1 y SW1?"
        )

        self.assertTrue(respuesta.ok)
        self.assertIn("Interfaces de R1 en estado running", respuesta.texto)
        self.assertIn("Interfaces de SW1 en estado running", respuesta.texto)
        self.assertEqual(self.llamadas, ["r1", "sw1"])
        self.assertNotIn("ether2", respuesta.texto)

    def test_down_combinadas(self):
        respuesta = self.construir().procesar(
            "¿Qué interfaces están desconectadas en R1 y SW1?"
        )

        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.resultado["estado_solicitado"], "down")
        self.assertEqual(set(respuesta.resultado["equipos"]), {"r1", "sw1"})

    def test_running_de_un_equipo_con_sinonimo_activas(self):
        respuesta = self.construir().procesar("¿Qué interfaces activas tiene R1?")

        self.assertTrue(respuesta.ok)
        self.assertIn("estado running", respuesta.texto)
        self.assertIn("ether1", respuesta.texto)

    def test_running_de_sw1(self):
        respuesta = self.construir().procesar(
            "¿Qué interfaces están en estado running en SW1?"
        )

        self.assertTrue(respuesta.ok)
        self.assertIn("Interfaces de SW1 en estado running", respuesta.texto)
        self.assertIn("ether1", respuesta.texto)
        self.assertNotIn("ether2", respuesta.texto)

    def test_fallo_de_conexion_identifica_el_equipo(self):
        respuesta = self.construir(
            errores={"sw1": {"tipo": "conexion", "mensaje": "sin ruta al equipo"}}
        ).procesar("¿Qué interfaces down están en R1 y SW1?")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "consulta_parcial")
        self.assertIn("No se pudo consultar SW1", respuesta.texto)
        self.assertIn("sin ruta al equipo", respuesta.texto)
        self.assertIn("ether2", respuesta.texto)  # R1 sí se consultó

    def test_sin_interfaces_en_estado_solicitado(self):
        respuesta = self.construir(
            datos={
                "r1": [
                    {"nombre": "ether1", "estado": "running", "ip": "", "comentario": ""}
                ],
                "sw1": INTERFACES["sw1"],
            }
        ).procesar("¿Qué interfaces down tiene R1?")

        self.assertTrue(respuesta.ok)
        self.assertIn("no se observaron interfaces en estado down", respuesta.texto)
        self.assertEqual(respuesta.resultado["salida"]["interfaces"], [])

    def test_sinonimos_y_estados_no_se_confunden(self):
        casos = (
            ("conectadas", "running"),
            ("activas", "running"),
            ("desconectadas", "down"),
            ("down", "down"),
            ("running", "running"),
            ("desactivadas", "disabled"),
        )
        for palabra, estado in casos:
            with self.subTest(palabra=palabra):
                filtro = extraer_filtro_interfaces_estado(
                    f"¿Qué interfaces {palabra} hay en R1?"
                )
                self.assertIsNotNone(filtro)
                self.assertEqual(filtro.estado, estado)


if __name__ == "__main__":
    unittest.main()
