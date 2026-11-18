"""Pruebas de agente/ejecutar.py: aprobación humana y ejecución sin SSH real."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from netmiko import NetmikoAuthenticationException

from agente.catalogo import (
    ACTUALIZAR_COMENTARIO,
    CONSULTAR_INTERFACES,
    Catalogo,
    Herramienta,
    catalogo_por_defecto,
)
from agente.ejecutar import Aprobacion, Resultado, aprobar, ejecutar
from agente.politicas import preparar
from config.ajustes import cargar_ajustes
from herramientas.comun import construir_comando_comentario
from tests.test_comun import ENTORNO_PRUEBA

COMENTARIO_DEMO = "P2-NetAgent-IA-Demo"


def salida_interfaces(comentario_ether4: str = "") -> str:
    """Salida de ``print detail`` con ether4 y su comentario actual."""
    marca = f" ;;; {comentario_ether4}" if comentario_ether4 else ""
    return f"""\
Flags: D - dynamic, X - disabled, R - running

 0  R name="ether1" type="ether" running=yes
 3 X {marca}
      name="ether4" default-name="ether4" type="ether"
"""


class SesionFalsa:
    """Sesión Netmiko simulada: lecturas en orden y escrituras registradas."""

    def __init__(self, lecturas=None, respuesta_escritura: str = "") -> None:
        self.lecturas = list(lecturas or [])
        self.respuesta_escritura = respuesta_escritura
        self.comandos: list[str] = []
        self.escrituras: list[str] = []
        self.desconectada = False

    def send_command(self, comando, **kwargs):
        self.comandos.append(comando)
        if not self.lecturas:
            raise AssertionError("lectura inesperada")
        return self.lecturas.pop(0)

    def send_command_timing(self, comando, **kwargs):
        self.comandos.append(comando)
        self.escrituras.append(comando)
        return self.respuesta_escritura

    def disconnect(self):
        self.desconectada = True


class FuncionFalsa:
    """Herramienta simulada que registra cómo la invocaron."""

    def __init__(self, salida=None) -> None:
        self.salida = {"ok": True, "changed": False, "error": None} if salida is None else salida
        self.llamadas: list[dict] = []

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        return self.salida


def catalogo_con(nombre: str, funcion) -> Catalogo:
    """Catálogo de un solo elemento con el esquema real y una función falsa."""
    real = catalogo_por_defecto().obtener(nombre)
    return Catalogo([Herramienta(definicion=real.definicion, funcion=funcion)])


class BaseEjecucion(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _lectura(self):
        return preparar(self.ajustes, CONSULTAR_INTERFACES, {"equipo": "r1"})

    def _escritura(self, comentario: str = COMENTARIO_DEMO, interfaz: str = "ether4"):
        return preparar(
            self.ajustes,
            ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": interfaz, "comentario": comentario},
        )


class PruebaLectura(BaseEjecucion):
    def test_la_lectura_se_ejecuta_sin_aprobacion(self):
        sesion = SesionFalsa([salida_interfaces("demo"), ""])
        with patch("herramientas.comun.ConnectHandler", return_value=sesion):
            resultado = ejecutar(self._lectura(), ajustes=self.ajustes)

        self.assertIsInstance(resultado, Resultado)
        self.assertTrue(resultado.ok)
        self.assertTrue(resultado.ejecutada)
        self.assertFalse(resultado.aprobada)
        self.assertIsNone(resultado.error)
        self.assertFalse(resultado.salida["changed"])
        self.assertEqual(resultado.salida["equipo"], "R1")
        self.assertEqual(resultado.salida["interfaces"][1]["comentario"], "demo")
        self.assertEqual(sesion.escrituras, [])
        self.assertTrue(sesion.desconectada)

    def test_la_lectura_no_escribe_aunque_se_presente_una_aprobacion(self):
        sesion = SesionFalsa([salida_interfaces("demo"), ""])
        with patch("herramientas.comun.ConnectHandler", return_value=sesion):
            resultado = ejecutar(
                self._lectura(), Aprobacion(herramienta=CONSULTAR_INTERFACES, clave="r1"),
                ajustes=self.ajustes,
            )

        self.assertTrue(resultado.ok)
        self.assertFalse(resultado.aprobada)
        self.assertEqual(sesion.escrituras, [])


class PruebaEscrituraSinAprobacion(BaseEjecucion):
    def test_sin_marca_no_ejecuta_ni_conecta(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = ejecutar(self._escritura(), ajustes=self.ajustes)

        self.assertFalse(resultado.ok)
        self.assertFalse(resultado.ejecutada)
        self.assertFalse(resultado.aprobada)
        self.assertIsNone(resultado.salida)
        self.assertEqual(resultado.error["tipo"], "aprobacion_requerida")
        mock.assert_not_called()

    def test_una_marca_de_otro_comentario_no_autoriza(self):
        accion = self._escritura()
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = ejecutar(
                accion, aprobar(self._escritura("otro texto")), ajustes=self.ajustes
            )

        self.assertEqual(resultado.error["tipo"], "aprobacion_invalida")
        self.assertFalse(resultado.ejecutada)
        mock.assert_not_called()

    def test_una_marca_de_otra_interfaz_o_equipo_no_autoriza(self):
        aprobaciones = (
            Aprobacion(
                herramienta=ACTUALIZAR_COMENTARIO,
                clave="r1",
                interfaz="ether2",
                comentario=COMENTARIO_DEMO,
            ),
            Aprobacion(
                herramienta=ACTUALIZAR_COMENTARIO,
                clave="sw1",
                interfaz="ether4",
                comentario=COMENTARIO_DEMO,
            ),
            Aprobacion(
                herramienta=CONSULTAR_INTERFACES,
                clave="r1",
                interfaz="ether4",
                comentario=COMENTARIO_DEMO,
            ),
        )
        for aprobacion in aprobaciones:
            with self.subTest(aprobacion=aprobacion.a_dict()):
                with patch("herramientas.comun.ConnectHandler") as mock:
                    resultado = ejecutar(
                        self._escritura(), aprobacion, ajustes=self.ajustes
                    )
                self.assertEqual(resultado.error["tipo"], "aprobacion_invalida")
                mock.assert_not_called()

    def test_una_marca_que_no_es_aprobacion_no_autoriza(self):
        for marca in (True, {"herramienta": ACTUALIZAR_COMENTARIO}, "sí", 1):
            with self.subTest(marca=marca):
                with patch("herramientas.comun.ConnectHandler") as mock:
                    resultado = ejecutar(
                        self._escritura(), marca, ajustes=self.ajustes
                    )
                self.assertEqual(resultado.error["tipo"], "aprobacion_invalida")
                mock.assert_not_called()

    def test_el_ajuste_desactivado_no_elimina_la_confirmacion(self):
        with patch.dict(
            os.environ, {**ENTORNO_PRUEBA, "NETAGENT_REQUIERE_APROBACION": "false"}
        ):
            ajustes = cargar_ajustes()
        self.assertFalse(ajustes.requiere_aprobacion)

        accion = preparar(
            ajustes,
            ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": COMENTARIO_DEMO},
        )
        self.assertTrue(accion.requiere_aprobacion)

        with patch("herramientas.comun.ConnectHandler") as mock:
            rechazado = ejecutar(accion, ajustes=ajustes)
        self.assertEqual(rechazado.error["tipo"], "aprobacion_requerida")
        mock.assert_not_called()


class PruebaEscrituraAprobada(BaseEjecucion):
    def test_con_marca_valida_escribe_y_verifica(self):
        sesion = SesionFalsa([salida_interfaces(""), salida_interfaces(COMENTARIO_DEMO)])
        accion = self._escritura()
        with patch("herramientas.comun.ConnectHandler", return_value=sesion):
            resultado = ejecutar(accion, aprobar(accion), ajustes=self.ajustes)

        self.assertTrue(resultado.ok)
        self.assertTrue(resultado.ejecutada)
        self.assertTrue(resultado.aprobada)
        self.assertTrue(resultado.salida["changed"])
        self.assertEqual(resultado.salida["comentario_anterior"], "")
        self.assertTrue(resultado.salida["verificacion"]["coincide"])
        self.assertEqual(
            sesion.escrituras,
            [construir_comando_comentario("ether4", COMENTARIO_DEMO)],
        )
        self.assertTrue(sesion.desconectada)

    def test_pasa_confirmacion_true_a_la_herramienta_existente(self):
        funcion = FuncionFalsa()
        accion = self._escritura()
        resultado = ejecutar(
            accion,
            aprobar(accion),
            catalogo=catalogo_con(ACTUALIZAR_COMENTARIO, funcion),
            ajustes=self.ajustes,
        )

        self.assertTrue(resultado.ok)
        self.assertTrue(resultado.aprobada)
        self.assertEqual(len(funcion.llamadas), 1)
        llamada = funcion.llamadas[0]
        self.assertIs(llamada["confirmacion"], True)
        self.assertIs(llamada["ajustes"], self.ajustes)
        self.assertEqual(llamada["equipo"], "r1")
        self.assertEqual(llamada["interfaz"], "ether4")
        self.assertEqual(llamada["comentario"], COMENTARIO_DEMO)

    def test_sin_marca_la_herramienta_no_se_invoca(self):
        funcion = FuncionFalsa()
        ejecutar(
            self._escritura(),
            catalogo=catalogo_con(ACTUALIZAR_COMENTARIO, funcion),
            ajustes=self.ajustes,
        )
        self.assertEqual(funcion.llamadas, [])

    def test_error_de_routeros_se_informa_sin_afirmar_cambio(self):
        sesion = SesionFalsa(
            [salida_interfaces("")], respuesta_escritura="failure: no such item"
        )
        accion = self._escritura()
        with patch("herramientas.comun.ConnectHandler", return_value=sesion):
            resultado = ejecutar(accion, aprobar(accion), ajustes=self.ajustes)

        self.assertFalse(resultado.ok)
        self.assertFalse(resultado.salida["changed"])
        self.assertEqual(resultado.error["tipo"], "modificacion_fallida")
        self.assertEqual(resultado.salida["respuesta_equipo"], "failure: no such item")


class PruebaErrores(BaseEjecucion):
    def test_error_de_conexion_se_devuelve_estructurado(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=NetmikoAuthenticationException("credenciales inválidas"),
        ):
            resultado = ejecutar(self._lectura(), ajustes=self.ajustes)

        self.assertFalse(resultado.ok)
        self.assertTrue(resultado.ejecutada)
        self.assertEqual(resultado.error["tipo"], "autenticacion")
        self.assertNotIn(ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"], json.dumps(resultado.a_dict()))

    def test_resultado_es_serializable_y_tiene_estructura_estable(self):
        sesion = SesionFalsa([salida_interfaces("demo"), ""])
        with patch("herramientas.comun.ConnectHandler", return_value=sesion):
            resultado = ejecutar(self._lectura(), ajustes=self.ajustes)

        datos = resultado.a_dict()
        self.assertEqual(
            list(datos),
            ["ok", "herramienta", "riesgo", "ejecutada", "aprobada", "salida", "error"],
        )
        self.assertEqual(datos["herramienta"], CONSULTAR_INTERFACES)
        self.assertEqual(datos["riesgo"], "lectura")
        json.dumps(datos, ensure_ascii=False)

    def test_rechaza_algo_que_no_es_una_accion(self):
        for valor in ("consultar_interfaces", {"equipo": "r1"}, None):
            with self.subTest(valor=valor):
                with self.assertRaises(TypeError):
                    ejecutar(valor, ajustes=self.ajustes)


if __name__ == "__main__":
    unittest.main()
