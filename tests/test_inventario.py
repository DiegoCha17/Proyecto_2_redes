"""Pruebas de consulta del inventario lógico, sin conexiones a MikroTik."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agente.cerebro import Cerebro, RegistroSesion
from agente.catalogo import CONSULTAR_INVENTARIO, catalogo_por_defecto
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


class PruebaConsultaInventario(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")

    def consultar(self, ajustes=None):
        cerebro = Cerebro(
            ajustes or self.ajustes,
            proveedor=None,
            catalogo=catalogo_por_defecto(),
            salida=lambda _texto: None,
            registro=self.registro,
        )
        return cerebro.procesar(
            "¿Qué dispositivos tienes configurados en tu inventario? "
            "Indica sus nombres, roles y direcciones IP."
        )

    def test_consulta_todos_los_dispositivos_configurados(self):
        respuesta = self.consultar()

        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.resultado["salida"]["ok"], True)
        dispositivos = respuesta.resultado["salida"]["dispositivos"]
        self.assertEqual([dispositivo["clave"] for dispositivo in dispositivos], ["r1", "sw1"])

    def test_muestra_nombre_rol_y_direccion_ip_reales(self):
        respuesta = self.consultar()

        self.assertIn("R1", respuesta.texto)
        self.assertIn("router", respuesta.texto)
        self.assertIn("10.60.30.202", respuesta.texto)
        self.assertIn("SW1", respuesta.texto)
        self.assertIn("switch", respuesta.texto)
        self.assertIn("10.60.30.204", respuesta.texto)

    def test_dispositivo_sin_ip_se_marca_sin_inventarla(self):
        sw1_sin_ip = replace(self.ajustes.dispositivos["sw1"], host="")
        ajustes = replace(
            self.ajustes,
            dispositivos={**self.ajustes.dispositivos, "sw1": sw1_sin_ip},
        )

        respuesta = self.consultar(ajustes)

        self.assertTrue(respuesta.ok)
        self.assertIn("SW1", respuesta.texto)
        self.assertIn("IP: no configurada", respuesta.texto)
        sw1 = respuesta.resultado["salida"]["dispositivos"][1]
        self.assertIsNone(sw1["ip"])

    def test_inventario_vacio_no_inventa_dispositivos(self):
        respuesta = self.consultar(replace(self.ajustes, dispositivos={}))

        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.resultado["salida"]["dispositivos"], [])
        self.assertIn("No hay dispositivos configurados", respuesta.texto)

    def test_no_muestra_dispositivos_que_no_estan_configurados(self):
        ajustes = replace(
            self.ajustes,
            dispositivos={"r1": self.ajustes.dispositivos["r1"]},
        )

        respuesta = self.consultar(ajustes)

        self.assertTrue(respuesta.ok)
        self.assertIn("R1", respuesta.texto)
        self.assertNotIn("SW1", respuesta.texto)
        self.assertEqual(
            respuesta.resultado["salida"]["dispositivos"],
            [{
                "clave": "r1",
                "nombre": "R1",
                "rol": "router",
                "ip": "10.60.30.202",
            }],
        )

    def test_es_lectura_y_no_abre_conexion(self):
        herramienta = catalogo_por_defecto().obtener(CONSULTAR_INVENTARIO)
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=AssertionError("el inventario no debe abrir SSH"),
        ):
            salida = herramienta.invocar({}, ajustes=self.ajustes)

        self.assertTrue(salida["ok"])
        self.assertFalse(salida["changed"])


if __name__ == "__main__":
    unittest.main()
