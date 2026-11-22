"""Pruebas de herramientas/actualizar_comentario.py sin SSH real."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException, ReadTimeout

from config.ajustes import cargar_ajustes
from herramientas.actualizar_comentario import (
    MAX_RESPUESTA_EQUIPO,
    actualizar_comentario,
    main,
)
from herramientas.comun import (
    MAX_COMENTARIO,
    ErrorHerramienta,
    construir_comando_comentario,
)
from tests.test_comun import ENTORNO_PRUEBA

COMENTARIO_DEMO = "P2-NetAgent-IA-Demo"


def salida_interfaces(comentario_ether4: str = "") -> str:
    """Salida reducida de RouterOS con ether4 y su comentario actual."""
    return f"""\
Flags: D - dynamic, X - disabled, R - running

 0  R name="ether1" type="ether" running=yes comment="WAN simulada"
 1  R name="ether2" type="ether" running=yes comment="LAN R1"
 3 X  name="ether4" type="ether" comment="{comentario_ether4}"
"""


def salida_interfaces_detalle(comentario_ether4: str = "") -> str:
    """Salida de ``print detail`` como la imprime R1.

    El comentario viaja en la línea ``;;;`` del propio registro y no como la
    propiedad ``comment=``; es el formato que el parser no reconocía.
    """
    marca = f' ;;; {comentario_ether4}' if comentario_ether4 else ""
    return f"""\
Flags: D - dynamic, X - disabled, R - running

 0  R name="ether1" type="ether" running=yes comment="WAN simulada"
 1  R name="ether2" type="ether" running=yes comment="LAN R1"
 3 X {marca}
      name="ether4" default-name="ether4" type="ether" mtu=1500
"""


class ConexionGuion:
    """Sesión simulada: lecturas en orden y registro de la escritura.

    La escritura debe llegar por ``send_command_timing``. Si llegara por
    ``send_command`` la prueba falla: ese era justamente el defecto (parada en
    el prompt repintado de RouterOS, salida vacía y error nunca detectado).
    """

    def __init__(
        self,
        lecturas: list[str] | None = None,
        fallos_lectura: set[int] | None = None,
        error_modificacion: Exception | None = None,
        respuesta_modificacion: str = "",
    ) -> None:
        self.lecturas = list(lecturas or [])
        self.fallos_lectura = fallos_lectura or set()
        self.error_modificacion = error_modificacion
        self.respuesta_modificacion = respuesta_modificacion
        self.lecturas_realizadas = 0
        self.comandos: list[str] = []
        self.modificaciones: list[str] = []
        self.orden: list[str] = []
        self.argumentos_escritura: dict = {}
        self.desconectada = False

    def send_command(self, comando, **kwargs):
        if comando.startswith("/interface set "):
            raise AssertionError(
                "la escritura debe usar send_command_timing, no send_command"
            )
        self.comandos.append(comando)
        self.orden.append("lectura")
        self.lecturas_realizadas += 1
        if self.lecturas_realizadas in self.fallos_lectura:
            raise ReadTimeout("lectura simulada agotada")
        if not self.lecturas:
            raise AssertionError("lectura inesperada")
        return self.lecturas.pop(0)

    def send_command_timing(self, comando, **kwargs):
        self.comandos.append(comando)
        self.orden.append("escritura")
        if not comando.startswith("/interface set "):
            raise AssertionError("escritura inesperada: solo el comentario autorizado")
        self.argumentos_escritura = dict(kwargs)
        if self.error_modificacion is not None:
            raise self.error_modificacion
        self.modificaciones.append(comando)
        return self.respuesta_modificacion

    def disconnect(self):
        self.desconectada = True


class PruebaValidacionComando(unittest.TestCase):
    def test_comando_solo_cambia_el_comentario(self):
        self.assertEqual(
            construir_comando_comentario("ether4", COMENTARIO_DEMO),
            '/interface set [find name="ether4"] comment="P2-NetAgent-IA-Demo"',
        )

    def test_rechaza_nombres_de_interfaz_peligrosos(self):
        for interfaz in (
            'ether4"] /system reboot',
            "ether 4",
            "ether4; /system reboot",
            "ether4\n/system reboot",
            "",
            "-ether4",
        ):
            with self.subTest(interfaz=interfaz):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    construir_comando_comentario(interfaz, "demo")
                self.assertEqual(contexto.exception.tipo, "parametros_invalidos")

    def test_rechaza_comentarios_que_rompen_la_cadena(self):
        for comentario in (
            'demo"; /system reboot',
            "demo\\",
            "linea\nnueva",
            "precio $x",
            "control\x00",
            "a" * (MAX_COMENTARIO + 1),
            None,
        ):
            with self.subTest(comentario=comentario):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    construir_comando_comentario("ether4", comentario)
                self.assertEqual(contexto.exception.tipo, "parametros_invalidos")

    def test_acepta_comentario_vacio_para_borrarlo(self):
        self.assertEqual(
            construir_comando_comentario("ether4", ""),
            '/interface set [find name="ether4"] comment=""',
        )


class PruebaActualizarComentario(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def test_configuracion_autoriza_ether2_y_ether4_en_r1(self):
        r1 = self.ajustes.dispositivo("r1")
        self.assertTrue(r1.interfaz_autorizada("ether2"))
        self.assertTrue(r1.interfaz_autorizada("ether4"))
        self.assertFalse(r1.interfaz_autorizada("ether1"))
        self.assertEqual(self.ajustes.dispositivo("sw1").interfaces_autorizadas, ())

    def test_equipo_no_autorizado_no_abre_conexion(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = actualizar_comentario(
                "pc1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "dispositivo_no_autorizado")
        self.assertFalse(resultado["changed"])
        self.assertIsNone(resultado["verificacion"])
        mock.assert_not_called()

    def test_interfaz_no_autorizada_no_abre_conexion(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = actualizar_comentario(
                "r1", "ether3", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "interfaz_no_autorizada")
        self.assertIn("ether2", resultado["error"]["mensaje"])
        self.assertIn("ether4", resultado["error"]["mensaje"])
        self.assertFalse(resultado["changed"])
        mock.assert_not_called()

    def test_parametros_invalidos_no_abren_conexion(self):
        casos = [
            ("r1", "", COMENTARIO_DEMO),
            ("r1", 'ether4"] /system reboot', COMENTARIO_DEMO),
            ("r1", "ether4", 'demo"; /system reboot'),
            ("r1", "ether4", "linea\nnueva"),
            ("r1", "ether4", None),
        ]
        for equipo, interfaz, comentario in casos:
            with self.subTest(interfaz=interfaz, comentario=comentario):
                with patch("herramientas.comun.ConnectHandler") as mock:
                    resultado = actualizar_comentario(
                        equipo, interfaz, comentario, True, self.ajustes
                    )
                self.assertFalse(resultado["ok"])
                self.assertEqual(resultado["error"]["tipo"], "parametros_invalidos")
                self.assertFalse(resultado["changed"])
                mock.assert_not_called()

    def test_sin_confirmacion_no_conecta_ni_cambia(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, False, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "confirmacion_requerida")
        self.assertFalse(resultado["confirmado"])
        self.assertFalse(resultado["changed"])
        self.assertEqual(resultado["comentario_solicitado"], COMENTARIO_DEMO)
        mock.assert_not_called()

    def test_comentario_identico_es_idempotente(self):
        falsa = ConexionGuion([salida_interfaces(COMENTARIO_DEMO)])
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["changed"])
        self.assertTrue(resultado["confirmado"])
        self.assertEqual(resultado["comentario_anterior"], COMENTARIO_DEMO)
        self.assertEqual(resultado["comentario_solicitado"], COMENTARIO_DEMO)
        self.assertEqual(resultado["verificacion"]["comentario_leido"], COMENTARIO_DEMO)
        self.assertTrue(resultado["verificacion"]["coincide"])
        self.assertIsNone(resultado["error"])
        self.assertEqual(falsa.modificaciones, [])
        self.assertTrue(falsa.desconectada)

    def test_cambio_exitoso_solo_tras_verificar(self):
        falsa = ConexionGuion(
            [salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)]
        )
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["changed"])
        self.assertTrue(resultado["confirmado"])
        self.assertEqual(resultado["equipo"], "R1")
        self.assertEqual(resultado["interfaz"], "ether4")
        self.assertEqual(resultado["comentario_anterior"], "")
        self.assertEqual(resultado["comentario_solicitado"], COMENTARIO_DEMO)
        self.assertEqual(
            falsa.modificaciones,
            [construir_comando_comentario("ether4", COMENTARIO_DEMO)],
        )
        self.assertEqual(len(falsa.comandos), 3)  # leer, cambiar, releer
        self.assertEqual(
            resultado["verificacion"],
            {"ok": True, "comentario_leido": COMENTARIO_DEMO, "coincide": True},
        )
        self.assertTrue(falsa.desconectada)

    def test_fallo_de_conexion_no_informa_cambio(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=NetmikoAuthenticationException("credenciales inválidas"),
        ):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "autenticacion")
        self.assertFalse(resultado["changed"])
        self.assertIsNone(resultado["verificacion"])
        self.assertNotIn(ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"], json.dumps(resultado))

    def test_timeout_de_conexion_se_informa(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=NetmikoTimeoutException("sin respuesta"),
        ):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "tiempo_agotado")
        self.assertFalse(resultado["changed"])

    def test_fallo_de_modificacion_no_informa_cambio(self):
        falsa = ConexionGuion(
            [salida_interfaces("")], error_modificacion=OSError("fallo simulado")
        )
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "modificacion_fallida")
        self.assertFalse(resultado["changed"])
        self.assertEqual(resultado["comentario_anterior"], "")
        self.assertIsNone(resultado["verificacion"])
        self.assertTrue(falsa.desconectada)

    def test_fallo_de_verificacion_no_afirma_exito(self):
        falsa = ConexionGuion(
            [salida_interfaces(""), salida_interfaces("otro comentario")]
        )
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "verificacion_fallida")
        self.assertFalse(resultado["changed"])
        self.assertEqual(
            resultado["verificacion"],
            {"ok": False, "comentario_leido": "otro comentario", "coincide": False},
        )
        self.assertTrue(falsa.desconectada)

    def test_relectura_fallida_no_afirma_exito(self):
        falsa = ConexionGuion([salida_interfaces("")], fallos_lectura={2})
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "verificacion_fallida")
        self.assertFalse(resultado["changed"])
        self.assertFalse(resultado["verificacion"]["ok"])
        self.assertIsNone(resultado["verificacion"]["comentario_leido"])
        self.assertTrue(falsa.desconectada)

    def test_lectura_inicial_fallida_no_modifica(self):
        falsa = ConexionGuion([], fallos_lectura={1})
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "tiempo_agotado")
        self.assertFalse(resultado["changed"])
        self.assertEqual(falsa.modificaciones, [])
        self.assertTrue(falsa.desconectada)

    def test_interfaz_autorizada_inexistente_no_se_modifica(self):
        salida_sin_ether4 = """\
Flags: D - dynamic, X - disabled, R - running

 0  R name="ether1" type="ether" running=yes comment="WAN simulada"
 1  R name="ether2" type="ether" running=yes comment="LAN R1"
"""
        falsa = ConexionGuion([salida_sin_ether4])
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "interfaz_inexistente")
        self.assertFalse(resultado["changed"])
        self.assertEqual(falsa.modificaciones, [])
        self.assertTrue(falsa.desconectada)


class PruebaComentarioFormatoRouterOS(unittest.TestCase):
    """La verificación debe leer el comentario tal como lo imprime RouterOS."""

    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _ejecutar(self, falsa: ConexionGuion, solicitado: str = COMENTARIO_DEMO) -> dict:
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            return actualizar_comentario(
                "r1", "ether4", solicitado, True, self.ajustes
            )

    def test_verificacion_reconoce_un_comentario_ya_aplicado(self):
        falsa = ConexionGuion([salida_interfaces_detalle(COMENTARIO_DEMO)])
        resultado = self._ejecutar(falsa)

        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["changed"])
        self.assertEqual(resultado["comentario_anterior"], COMENTARIO_DEMO)
        self.assertTrue(resultado["verificacion"]["coincide"])
        self.assertEqual(falsa.modificaciones, [])  # idempotente: no escribió

    def test_verificacion_tras_escribir_con_formato_routeros(self):
        falsa = ConexionGuion(
            [salida_interfaces_detalle(""), salida_interfaces_detalle(COMENTARIO_DEMO)]
        )
        resultado = self._ejecutar(falsa)

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["changed"])
        self.assertEqual(
            resultado["verificacion"]["comentario_leido"], COMENTARIO_DEMO
        )
        self.assertEqual(len(falsa.modificaciones), 1)

    def test_cambio_sobre_un_comentario_previo_visible_como_marca(self):
        falsa = ConexionGuion(
            [salida_interfaces_detalle("anterior"), salida_interfaces_detalle("nuevo")]
        )
        resultado = self._ejecutar(falsa, solicitado="nuevo")

        self.assertEqual(resultado["comentario_anterior"], "anterior")
        self.assertTrue(resultado["changed"])
        self.assertEqual(resultado["verificacion"]["comentario_leido"], "nuevo")

    def test_interfaz_sin_comentario_previo_no_inventa_texto(self):
        falsa = ConexionGuion(
            [salida_interfaces_detalle(""), salida_interfaces_detalle(COMENTARIO_DEMO)]
        )
        resultado = self._ejecutar(falsa)

        self.assertEqual(resultado["comentario_anterior"], "")
        self.assertTrue(resultado["changed"])


class PruebaEscrituraRouterOS(unittest.TestCase):
    """Corrección del fallo real: ``send_command`` cortaba en el prompt repintado."""

    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _ejecutar(self, falsa: ConexionGuion) -> dict:
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            return actualizar_comentario(
                "r1", "ether4", COMENTARIO_DEMO, True, self.ajustes
            )

    def test_escritura_usa_send_command_timing_y_respeta_el_orden(self):
        falsa = ConexionGuion(
            [salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)]
        )
        resultado = self._ejecutar(falsa)

        self.assertTrue(resultado["ok"])
        self.assertEqual(falsa.orden, ["lectura", "escritura", "lectura"])
        self.assertEqual(falsa.lecturas_realizadas, 2)
        self.assertEqual(
            falsa.modificaciones,
            [construir_comando_comentario("ether4", COMENTARIO_DEMO)],
        )
        self.assertIn("last_read", falsa.argumentos_escritura)
        self.assertGreater(falsa.argumentos_escritura["last_read"], 0)
        self.assertTrue(falsa.desconectada)

    def test_detecta_errores_devueltos_por_routeros(self):
        casos = (
            "failure: no such item",
            "bad command name set (line 1 column 1)",
            "input does not match any value of comment",
            "syntax error at 'comment'",
            "expected end of command (line 1 column 60)",
            "action failed",
        )
        for salida in casos:
            with self.subTest(salida=salida):
                falsa = ConexionGuion(
                    [salida_interfaces("")], respuesta_modificacion=salida
                )
                resultado = self._ejecutar(falsa)

                self.assertFalse(resultado["ok"])
                self.assertEqual(resultado["error"]["tipo"], "modificacion_fallida")
                self.assertFalse(resultado["changed"])
                self.assertIn("RouterOS rechazó", resultado["error"]["mensaje"])
                self.assertEqual(resultado["respuesta_equipo"], salida)
                self.assertEqual(falsa.lecturas_realizadas, 1)  # no se releyó
                self.assertTrue(falsa.desconectada)

    def test_la_relectura_es_la_que_autoriza_el_cambio(self):
        casos = (
            (salida_interfaces(COMENTARIO_DEMO), True),
            (salida_interfaces("otro comentario"), False),
        )
        for posterior, esperado in casos:
            with self.subTest(posterior=posterior, esperado=esperado):
                falsa = ConexionGuion([salida_interfaces(""), posterior])
                resultado = self._ejecutar(falsa)

                self.assertEqual(falsa.lecturas_realizadas, 2)
                self.assertEqual(resultado["changed"], esperado)
                self.assertEqual(resultado["ok"], esperado)

    def test_salida_vacia_no_se_confunde_con_error(self):
        falsa = ConexionGuion(
            [salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)]
        )
        resultado = self._ejecutar(falsa)

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["changed"])
        self.assertIsNone(resultado["respuesta_equipo"])

    def test_respuesta_equipo_se_recorta_a_300_caracteres(self):
        falsa = ConexionGuion(
            [salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)],
            respuesta_modificacion="x" * 500,
        )
        resultado = self._ejecutar(falsa)

        self.assertTrue(resultado["ok"])
        self.assertEqual(len(resultado["respuesta_equipo"]), MAX_RESPUESTA_EQUIPO)

    def test_cierra_la_conexion_en_todos_los_caminos(self):
        escenarios = {
            "cambio verificado": ConexionGuion(
                [salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)]
            ),
            "rechazo de routeros": ConexionGuion(
                [salida_interfaces("")], respuesta_modificacion="failure: no such item"
            ),
            "verificacion fallida": ConexionGuion(
                [salida_interfaces(""), salida_interfaces("otro comentario")]
            ),
            "lectura inicial fallida": ConexionGuion([], fallos_lectura={1}),
            "excepcion al escribir": ConexionGuion(
                [salida_interfaces("")], error_modificacion=OSError("fallo simulado")
            ),
        }
        for nombre, falsa in escenarios.items():
            with self.subTest(escenario=nombre):
                self._ejecutar(falsa)
                self.assertTrue(falsa.desconectada)


class PruebaCLI(unittest.TestCase):
    def test_cli_traduce_confirmacion_y_sale_cero(self):
        respuesta = {
            "ok": True,
            "equipo": "R1",
            "interfaz": "ether4",
            "comentario_anterior": "",
            "comentario_solicitado": COMENTARIO_DEMO,
            "changed": True,
            "confirmado": True,
            "verificacion": {"ok": True, "comentario_leido": COMENTARIO_DEMO, "coincide": True},
            "error": None,
        }
        with patch(
            "herramientas.actualizar_comentario.actualizar_comentario",
            return_value=respuesta,
        ) as mock:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                codigo = main(["r1", "ether4", COMENTARIO_DEMO, "--confirmacion"])

        self.assertEqual(codigo, 0)
        self.assertEqual(json.loads(buffer.getvalue()), respuesta)
        self.assertTrue(mock.call_args.kwargs["confirmacion"])

    def test_cli_sin_confirmacion_pasa_falso_y_sale_uno(self):
        respuesta = {
            "ok": False,
            "equipo": "R1",
            "interfaz": "ether4",
            "comentario_anterior": None,
            "comentario_solicitado": COMENTARIO_DEMO,
            "changed": False,
            "confirmado": False,
            "verificacion": None,
            "error": {"tipo": "confirmacion_requerida", "mensaje": "falta confirmación"},
        }
        with patch(
            "herramientas.actualizar_comentario.actualizar_comentario",
            return_value=respuesta,
        ) as mock:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                codigo = main(["r1", "ether4", COMENTARIO_DEMO])

        self.assertEqual(codigo, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])
        self.assertFalse(mock.call_args.kwargs["confirmacion"])


if __name__ == "__main__":
    unittest.main()
