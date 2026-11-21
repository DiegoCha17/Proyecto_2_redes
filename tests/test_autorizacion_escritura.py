"""Pruebas del flujo de autorización humana para escrituras, sin routers reales."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import RespuestaModelo
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


class DispositivoSimulado:
    def __init__(self) -> None:
        self.comentario = "P2-NetAgent-IA-Demo"
        self.escrituras: list[dict] = []
        self.no_aplicar = False

    def leer(self, equipo: str, **_opciones):
        return {
            "ok": True,
            "equipo": equipo.upper(),
            "interfaces": [
                {
                    "nombre": "ether4",
                    "estado": "running",
                    "ip": "",
                    "comentario": self.comentario,
                }
            ],
            "changed": False,
            "error": None,
        }

    def escribir(self, **argumentos):
        self.escrituras.append(dict(argumentos))
        if not self.no_aplicar:
            self.comentario = argumentos["comentario"]
        return {
            "ok": True,
            "equipo": "R1",
            "interfaz": argumentos["interfaz"],
            "comentario_anterior": "P2-NetAgent-IA-Demo",
            "comentario_solicitado": argumentos["comentario"],
            "changed": True,
            "confirmado": argumentos.get("confirmacion", False),
            "verificacion": {"ok": True, "coincide": True},
            "error": None,
        }


class FlujoAutorizacionEscritura(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.vistas: list[str] = []
        self.preguntas: list[object] = []
        self.dispositivo = DispositivoSimulado()

        real = catalogo_por_defecto()
        self.catalogo = Catalogo(
            [
                Herramienta(
                    real.obtener(CONSULTAR_INTERFACES).definicion,
                    self.dispositivo.leer,
                ),
                Herramienta(
                    real.obtener(ACTUALIZAR_COMENTARIO).definicion,
                    self.dispositivo.escribir,
                ),
            ]
        )
        self.registro = RegistroSesion(Path(temporal.name), modelo="prueba")

    def construir(self, confirmar):
        def confirmar_registrada(plan):
            self.preguntas.append(plan)
            return confirmar(plan)

        return Cerebro(
            self.ajustes,
            proveedor=None,
            catalogo=self.catalogo,
            salida=self.vistas.append,
            confirmar=confirmar_registrada,
            registro=self.registro,
        )

    def test_escritura_autorizada_queda_pendiente_y_no_se_ejecuta_antes(self):
        def rechazar(plan):
            self.assertEqual(plan.accion.clave, "r1")
            self.assertEqual(plan.accion.interfaz, "ether4")
            self.assertEqual(self.dispositivo.escrituras, [])
            return False

        respuesta = self.construir(rechazar).procesar(
            "Cambia el comentario de ether4 en R1 a PRUEBA_AUTORIZACION"
        )

        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertFalse(respuesta.ok)
        self.assertEqual(len(self.preguntas), 1)
        self.assertEqual(self.dispositivo.escrituras, [])
        vista = "\n".join(self.vistas)
        self.assertIn("Vista previa", vista)
        self.assertIn("P2-NetAgent-IA-Demo", vista)
        self.assertIn("PRUEBA_AUTORIZACION", vista)

    def test_se_ejecuta_solo_despues_de_aprobar_y_se_verifica(self):
        respuesta = self.construir(lambda _plan: True).procesar(
            "Cambia el comentario de ether4 en R1 a PRUEBA_AUTORIZACION"
        )

        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.tipo, "modificacion")
        self.assertEqual(len(self.dispositivo.escrituras), 1)
        self.assertIs(self.dispositivo.escrituras[0]["confirmacion"], True)
        self.assertEqual(self.dispositivo.comentario, "PRUEBA_AUTORIZACION")
        self.assertIn("lectura independiente lo confirmó", respuesta.texto)

    def test_rechazo_cancela_sin_modificar(self):
        respuesta = self.construir(lambda _plan: False).procesar(
            "Cambia el comentario de ether4 en R1 a OTRO_VALOR"
        )

        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertEqual(self.dispositivo.escrituras, [])
        self.assertEqual(self.dispositivo.comentario, "P2-NetAgent-IA-Demo")

    def test_sw1_no_tiene_permisos_de_escritura(self):
        respuesta = self.construir(lambda _plan: True).procesar(
            "Cambia el comentario de ether4 en SW1 a NO_PERMITIDO"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "interfaz_no_autorizada")
        self.assertEqual(self.preguntas, [])
        self.assertEqual(self.dispositivo.escrituras, [])

    def test_r1_rechaza_interfaz_no_autorizada(self):
        respuesta = self.construir(lambda _plan: True).procesar(
            "Cambia el comentario de ether1 en R1 a NO_PERMITIDO"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "interfaz_no_autorizada")
        self.assertEqual(self.preguntas, [])
        self.assertEqual(self.dispositivo.escrituras, [])

    def test_no_afirma_cambio_si_la_lectura_posterior_no_coincide(self):
        self.dispositivo.no_aplicar = True
        respuesta = self.construir(lambda _plan: True).procesar(
            "Cambia el comentario de ether4 en R1 a NO_CONFIRMADO"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "verificacion_fallida")
        self.assertNotIn("quedó como", respuesta.texto)
        self.assertIn("no se informa como aplicado", respuesta.texto)

    def test_texto_del_modelo_no_puede_sustituir_la_propuesta_de_escritura(self):
        class ProveedorTexto:
            def responder_historial(self, _mensajes, _herramientas):
                return RespuestaModelo(
                    texto=(
                        "La interfaz ha sido actualizada. La aplicación solicitará "
                        "aprobación humana."
                    )
                )

        cerebro = Cerebro(
            self.ajustes,
            proveedor=ProveedorTexto(),
            catalogo=self.catalogo,
            salida=self.vistas.append,
            confirmar=lambda _plan: True,
            registro=self.registro,
        )
        respuesta = cerebro.procesar(
            "Cambia el comentario de ether4 en R1 a PRUEBA_AUTORIZACION"
        )

        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "respuesta_invalida")
        self.assertIn("no se mostró una aprobación", respuesta.texto)
        self.assertEqual(self.dispositivo.escrituras, [])


if __name__ == "__main__":
    unittest.main()
