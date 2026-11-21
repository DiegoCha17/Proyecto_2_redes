"""Pruebas del diagnóstico conjunto basado en lecturas simuladas."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import extraer_solicitud_diagnostico
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


DATOS = {
    "r1": [
        {"nombre": "ether1", "estado": "running", "ip": "10.0.0.1/24", "comentario": "WAN"},
        {"nombre": "ether4", "estado": "down", "ip": "", "comentario": "P2-NetAgent-IA-Demo"},
        {"nombre": "ether5", "estado": "disabled", "ip": "", "comentario": "reserva"},
    ],
    "sw1": [
        {"nombre": "ether1", "estado": "running", "ip": "192.168.10.2/24", "comentario": "uplink"},
        {"nombre": "ether2", "estado": "down", "ip": "", "comentario": ""},
    ],
}


class DiagnosticoConjunto(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")
        self.respuestas = {clave: [dict(item) for item in datos] for clave, datos in DATOS.items()}
        self.errores: dict[str, dict] = {}
        self.escrituras: list[dict] = []

        real = catalogo_por_defecto()

        def leer(equipo: str, **_opciones):
            if equipo in self.errores:
                return {
                    "ok": False,
                    "equipo": equipo.upper(),
                    "interfaces": [],
                    "changed": False,
                    "error": self.errores[equipo],
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

    def test_reconoce_diagnostico_de_dos_dispositivos(self):
        solicitud = extraer_solicitud_diagnostico(
            "¿Qué problemas o situaciones importantes detectas en R1 y SW1?"
        )

        self.assertIsNotNone(solicitud)
        self.assertEqual(solicitud.equipos, ("r1", "sw1"))

    def test_diagnostico_conjunto_compara_datos_reales(self):
        respuesta = self.construir().procesar(
            "¿Qué problemas o situaciones importantes detectas en R1 y SW1?"
        )

        self.assertTrue(respuesta.ok)
        self.assertIn("Resumen comparativo", respuesta.texto)
        self.assertIn("R1: running=1, down=1, disabled=1", respuesta.texto)
        self.assertIn("SW1: running=1, down=1, disabled=0", respuesta.texto)
        self.assertIn("con IP=1, sin IP=2", respuesta.texto)
        self.assertIn("con IP=1, sin IP=1", respuesta.texto)
        self.assertIn("P2-NetAgent-IA-Demo", respuesta.texto)
        self.assertIn("Situación que merece revisión", respuesta.texto)
        self.assertIn("no confirma una avería", respuesta.texto)

    def test_diagnostico_de_un_dispositivo(self):
        respuesta = self.construir().procesar(
            "¿Qué problemas detectas en R1?"
        )

        self.assertTrue(respuesta.ok)
        self.assertIn("Análisis de R1", respuesta.texto)
        self.assertNotIn("SW1:", respuesta.texto)
        self.assertIn("ether5", respuesta.texto)

    def test_fallo_de_un_dispositivo_continua_con_el_otro(self):
        self.errores["r1"] = {"tipo": "conexion", "mensaje": "sin ruta a R1"}

        respuesta = self.construir().procesar(
            "¿Qué problemas o situaciones importantes detectas en R1 y SW1?"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "consulta_parcial")
        self.assertIn("No se pudo analizar R1", respuesta.texto)
        self.assertIn("sin ruta a R1", respuesta.texto)
        self.assertIn("Análisis de SW1", respuesta.texto)

    def test_resultado_incompleto_se_informa_sin_conclusion_inventada(self):
        self.respuestas["sw1"] = [
            {"nombre": "ether1", "ip": "192.168.10.2/24", "comentario": "uplink"}
        ]

        respuesta = self.construir().procesar(
            "¿Qué problemas detectas en SW1?"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "respuesta_incompleta")
        self.assertIn("sin estado reconocido", respuesta.texto)
        self.assertNotIn("averiada", respuesta.texto.lower())

    def test_no_inventa_datos_y_no_ejecuta_escrituras(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=AssertionError("el diagnóstico simulado no debe abrir SSH"),
        ):
            respuesta = self.construir().procesar(
                "¿Qué problemas o situaciones importantes detectas en R1 y SW1?"
            )

        self.assertTrue(respuesta.ok)
        self.assertNotIn("ether99", respuesta.texto)
        self.assertNotIn("configuración incorrecta", respuesta.texto.lower())
        self.assertEqual(self.escrituras, [])


if __name__ == "__main__":
    unittest.main()
