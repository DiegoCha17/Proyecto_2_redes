"""Pruebas de las políticas deterministas de autorización (sin SSH)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES
from agente.politicas import (
    preparar,
    requiere_aprobacion_humana,
    resolver_equipo,
)
from config.ajustes import cargar_ajustes
from herramientas.comun import MAX_COMENTARIO, ErrorHerramienta
from tests.test_comun import ENTORNO_PRUEBA

COMENTARIO_DEMO = "P2-NetAgent-IA-Demo"


class PruebaPoliticaAprobacion(unittest.TestCase):
    def test_solo_la_escritura_exige_aprobacion(self):
        self.assertFalse(requiere_aprobacion_humana("lectura"))
        self.assertTrue(requiere_aprobacion_humana("escritura"))
        self.assertFalse(requiere_aprobacion_humana("riesgo_inventado"))

    def test_el_ajuste_del_entorno_no_cambia_la_politica(self):
        with patch.dict(
            os.environ, {**ENTORNO_PRUEBA, "NETAGENT_REQUIERE_APROBACION": "false"}
        ):
            ajustes = cargar_ajustes()
        self.assertFalse(ajustes.requiere_aprobacion)
        self.assertTrue(requiere_aprobacion_humana("escritura"))


class PruebaPrepararAcciones(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _preparar(self, herramienta: str, argumentos=None):
        return preparar(self.ajustes, herramienta, argumentos)

    def test_la_lectura_se_autoriza_en_r1_y_sw1(self):
        for clave, nombre in (("r1", "R1"), ("sw1", "SW1"), ("SW1", "SW1")):
            with self.subTest(clave=clave):
                accion = self._preparar(CONSULTAR_INTERFACES, {"equipo": clave})
                self.assertEqual(accion.herramienta, CONSULTAR_INTERFACES)
                self.assertEqual(accion.riesgo, "lectura")
                self.assertFalse(accion.requiere_aprobacion)
                self.assertFalse(accion.es_escritura)
                self.assertEqual(accion.clave, clave.lower())
                self.assertEqual(accion.equipo, nombre)
                self.assertEqual(dict(accion.argumentos), {"equipo": clave.lower()})

    def test_la_escritura_se_autoriza_solo_en_ether4_de_r1(self):
        accion = self._preparar(
            ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": COMENTARIO_DEMO},
        )
        self.assertEqual(accion.riesgo, "escritura")
        self.assertTrue(accion.es_escritura)
        self.assertTrue(accion.requiere_aprobacion)
        self.assertEqual(accion.clave, "r1")
        self.assertEqual(accion.equipo, "R1")
        self.assertEqual(accion.interfaz, "ether4")
        self.assertEqual(accion.comentario, COMENTARIO_DEMO)
        self.assertIn("ether4", accion.descripcion)
        self.assertIn(COMENTARIO_DEMO, accion.descripcion)

    def test_la_accion_es_inmutable(self):
        accion = self._preparar(CONSULTAR_INTERFACES, {"equipo": "r1"})
        with self.assertRaises(TypeError):
            accion.argumentos["equipo"] = "sw1"
        self.assertEqual(accion.a_dict()["argumentos"], {"equipo": "r1"})

    def test_equipo_no_autorizado_se_rechaza(self):
        casos = ("pc1", "servidor-wan", "10.60.30.202")
        for equipo in casos:
            with self.subTest(equipo=equipo):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(CONSULTAR_INTERFACES, {"equipo": equipo})
                self.assertEqual(
                    contexto.exception.tipo, "dispositivo_no_autorizado"
                )

    def test_equipo_vacio_o_ausente_es_parametro_invalido(self):
        for argumentos in ({}, {"equipo": ""}, {"equipo": "   "}, {"equipo": None}):
            with self.subTest(argumentos=argumentos):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(CONSULTAR_INTERFACES, argumentos)
                self.assertIn(
                    contexto.exception.tipo,
                    {"parametros_invalidos", "argumentos_invalidos"},
                )

    def test_sw1_no_permite_escrituras(self):
        for interfaz in ("ether1", "ether4", "bridge"):
            with self.subTest(interfaz=interfaz):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(
                        ACTUALIZAR_COMENTARIO,
                        {"equipo": "sw1", "interfaz": interfaz, "comentario": "demo"},
                    )
                self.assertEqual(contexto.exception.tipo, "interfaz_no_autorizada")
                self.assertIn("(ninguna)", contexto.exception.mensaje)

    def test_r1_solo_permite_ether4(self):
        with self.assertRaises(ErrorHerramienta) as contexto:
            self._preparar(
                ACTUALIZAR_COMENTARIO,
                {"equipo": "r1", "interfaz": "ether2", "comentario": "demo"},
            )
        self.assertEqual(contexto.exception.tipo, "interfaz_no_autorizada")
        self.assertIn("ether4", contexto.exception.mensaje)

    def test_interfaz_con_caracteres_peligrosos_se_rechaza(self):
        for interfaz in (
            'ether4"] /system reboot',
            "ether 4",
            "ether4; /system reboot",
            "ether4\n/system reboot",
        ):
            with self.subTest(interfaz=interfaz):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(
                        ACTUALIZAR_COMENTARIO,
                        {"equipo": "r1", "interfaz": interfaz, "comentario": "demo"},
                    )
                self.assertEqual(contexto.exception.tipo, "parametros_invalidos")

    def test_comentario_invalido_se_rechaza(self):
        casos = (
            'demo"; /system reboot',
            "demo\\",
            "linea\nnueva",
            "precio $x",
            "a" * (MAX_COMENTARIO + 1),
        )
        for comentario in casos:
            with self.subTest(comentario=comentario):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(
                        ACTUALIZAR_COMENTARIO,
                        {"equipo": "r1", "interfaz": "ether4", "comentario": comentario},
                    )
                self.assertEqual(contexto.exception.tipo, "parametros_invalidos")

    def test_comentario_vacio_se_admite_para_borrarlo(self):
        accion = self._preparar(
            ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": ""},
        )
        self.assertEqual(accion.comentario, "")

    def test_herramienta_fuera_del_catalogo_se_rechaza(self):
        for herramienta in ("reiniciar_equipo", "ejecutar_comando", ""):
            with self.subTest(herramienta=herramienta):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._preparar(herramienta, {"equipo": "r1"})
                self.assertEqual(
                    contexto.exception.tipo, "herramienta_no_autorizada"
                )

    def test_argumentos_de_mas_se_rechazan(self):
        with self.assertRaises(ErrorHerramienta) as contexto:
            self._preparar(
                ACTUALIZAR_COMENTARIO,
                {
                    "equipo": "r1",
                    "interfaz": "ether4",
                    "comentario": "demo",
                    "comando": "/system reboot",
                },
            )
        self.assertEqual(contexto.exception.tipo, "argumentos_invalidos")

    def test_preparar_nunca_abre_conexion(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=AssertionError("no debe abrirse una sesión"),
        ):
            self._preparar(CONSULTAR_INTERFACES, {"equipo": "r1"})
            self._preparar(
                ACTUALIZAR_COMENTARIO,
                {"equipo": "r1", "interfaz": "ether4", "comentario": COMENTARIO_DEMO},
            )

    def test_resolver_equipo_devuelve_el_dispositivo_del_inventario(self):
        dispositivo = resolver_equipo(self.ajustes, " R1 ")
        self.assertEqual(dispositivo.nombre, "R1")
        self.assertEqual(dispositivo.host, "10.60.30.202")
        self.assertEqual(dispositivo.interfaces_autorizadas, ("ether4",))


if __name__ == "__main__":
    unittest.main()
