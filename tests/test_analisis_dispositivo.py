"""Pruebas del análisis general de dispositivos con respuestas simuladas."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


DATOS = {
    "r1": [
        {"nombre": "ether1", "estado": "running", "ip": "10.0.0.1/24", "comentario": "WAN"},
        {"nombre": "ether4", "estado": "down", "ip": "", "comentario": "P2-NetAgent-IA-Demo"},
    ],
    "sw1": [
        {"nombre": "ether1", "estado": "running", "ip": "192.168.10.2/24", "comentario": "uplink"},
        {"nombre": "ether2", "estado": "down", "ip": "", "comentario": ""},
    ],
}


class AnalisisDispositivo(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")
        self.escrituras: list[dict] = []
        self.respuestas = dict(DATOS)
        self.error: dict | None = None

        real = catalogo_por_defecto()

        def leer(equipo: str, **_opciones):
            if self.error is not None:
                return {
                    "ok": False,
                    "equipo": equipo.upper(),
                    "interfaces": [],
                    "changed": False,
                    "error": self.error,
                }
            return {
                "ok": True,
                "equipo": equipo.upper(),
                "interfaces": [dict(item) for item in self.respuestas[equipo]],
                "changed": False,
                "error": None,
            }

        def escribir(**argumentos):
            self.escrituras.append(dict(argumentos))
            return {"ok": True, "changed": True, "error": None}

        self.catalogo = Catalogo(
            [
                Herramienta(real.obtener(CONSULTAR_INTERFACES).definicion, leer),
                Herramienta(real.obtener(ACTUALIZAR_COMENTARIO).definicion, escribir),
            ]
        )

    def construir(self):
        return Cerebro(
            self.ajustes,
            proveedor=None,
            catalogo=self.catalogo,
            salida=lambda _texto: None,
            confirmar=lambda _plan: True,
            registro=self.registro,
        )

    def test_analiza_r1_con_datos_reales_de_interfaces(self):
        respuesta = self.construir().procesar("Analiza R1")

        self.assertTrue(respuesta.ok)
        self.assertIn("Análisis de R1", respuesta.texto)
        self.assertIn("Activas (running): ether1", respuesta.texto)
        self.assertIn("Desconectadas (down): ether4", respuesta.texto)
        self.assertIn("10.0.0.1/24", respuesta.texto)
        self.assertIn("P2-NetAgent-IA-Demo", respuesta.texto)

    def test_analiza_sw1_sin_confundirlo_con_inventario(self):
        respuesta = self.construir().procesar("Analiza SW1")

        self.assertTrue(respuesta.ok)
        self.assertIn("Análisis de SW1", respuesta.texto)
        self.assertIn("ether1", respuesta.texto)
        self.assertIn("ether2", respuesta.texto)
        self.assertIn("192.168.10.2/24", respuesta.texto)

    def test_error_de_conexion_indica_que_no_pudo_obtenerse(self):
        self.error = {"tipo": "conexion", "mensaje": "sin ruta al equipo"}

        respuesta = self.construir().procesar("Analiza R1")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "conexion")
        self.assertIn("No se pudo analizar R1", respuesta.texto)
        self.assertIn("sin ruta al equipo", respuesta.texto)

    def test_resultado_incompleto_no_se_presenta_como_diagnostico_completo(self):
        self.respuestas["r1"] = [
            {"nombre": "ether1", "ip": "10.0.0.1/24", "comentario": "WAN"}
        ]

        respuesta = self.construir().procesar("Analiza R1")

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "respuesta_incompleta")
        self.assertIn("sin estado reconocido", respuesta.texto)
        self.assertIn("IP 10.0.0.1/24", respuesta.texto)

    def test_analisis_no_ejecuta_herramientas_de_escritura(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=AssertionError("el análisis simulado no debe abrir SSH"),
        ):
            respuesta = self.construir().procesar("Analiza R1")

        self.assertTrue(respuesta.ok)
        self.assertEqual(self.escrituras, [])

    def test_no_inventa_interfaces_ni_problemas(self):
        respuesta = self.construir().procesar("Analiza R1")

        self.assertNotIn("ether99", respuesta.texto)
        self.assertNotIn("configuración incorrecta", respuesta.texto.lower())
        self.assertEqual(
            respuesta.resultado["salida"]["interfaces"], DATOS["r1"]
        )


if __name__ == "__main__":
    unittest.main()
