"""Pruebas del planificador: vista previa y cancelación sin tocar la red."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from agente.catalogo import (
    ACTUALIZAR_COMENTARIO,
    CONSULTAR_INTERFACES,
    Catalogo,
    Herramienta,
    catalogo_por_defecto,
)
from agente.ejecutar import Resultado
from agente.planificador import Plan, cancelar, planificar
from config.ajustes import cargar_ajustes
from herramientas.comun import ErrorHerramienta
from tests.test_comun import ENTORNO_PRUEBA

COMENTARIO_DEMO = "P2-NetAgent-IA-Demo"

SIN_CONEXION = AssertionError("la planificación no debe abrir una sesión SSH")


class FuncionFalsa:
    def __init__(self) -> None:
        self.llamadas: list[dict] = []

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        return {"ok": True, "error": None}


def catalogo_con(nombre: str, funcion) -> Catalogo:
    real = catalogo_por_defecto().obtener(nombre)
    return Catalogo([Herramienta(definicion=real.definicion, funcion=funcion)])


class PruebaPlanLectura(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def test_plan_de_lectura_no_pide_autorizacion(self):
        plan = planificar(self.ajustes, CONSULTAR_INTERFACES, {"equipo": "sw1"})

        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.herramienta, CONSULTAR_INTERFACES)
        self.assertEqual(plan.riesgo, "lectura")
        self.assertFalse(plan.es_escritura)
        self.assertFalse(plan.requiere_autorizacion)
        self.assertEqual(plan.accion.equipo, "SW1")

    def test_vista_previa_de_lectura_avisa_que_no_cambia_nada(self):
        vista = planificar(self.ajustes, CONSULTAR_INTERFACES, {"equipo": "r1"}).vista_previa

        self.assertEqual(vista["herramienta"], CONSULTAR_INTERFACES)
        self.assertEqual(vista["equipo"], "R1")
        self.assertEqual(vista["argumentos"], {"equipo": "r1"})
        self.assertFalse(vista["requiere_aprobacion"])
        self.assertIn("Ninguno", vista["efecto"])
        self.assertNotIn("comentario_anterior", vista)

    def test_planificar_no_conecta_ni_ejecuta(self):
        funcion = FuncionFalsa()
        with patch("herramientas.comun.ConnectHandler", side_effect=SIN_CONEXION):
            planificar(
                self.ajustes,
                CONSULTAR_INTERFACES,
                {"equipo": "r1"},
                catalogo=catalogo_con(CONSULTAR_INTERFACES, funcion),
            )
        self.assertEqual(funcion.llamadas, [])


class PruebaPlanEscritura(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _plan(self, **argumentos):
        datos = {"equipo": "r1", "interfaz": "ether4", "comentario": COMENTARIO_DEMO}
        datos.update(argumentos)
        return planificar(self.ajustes, ACTUALIZAR_COMENTARIO, datos)

    def test_plan_de_escritura_exige_aprobacion(self):
        plan = self._plan()

        self.assertTrue(plan.es_escritura)
        self.assertTrue(plan.requiere_autorizacion)
        self.assertTrue(plan.vista_previa["requiere_aprobacion"])
        self.assertTrue(any("aprobación humana" in paso for paso in plan.pasos))

    def test_vista_previa_describe_el_efecto_sin_leer_el_equipo(self):
        with patch("herramientas.comun.ConnectHandler", side_effect=SIN_CONEXION):
            plan = self._plan()

        vista = plan.vista_previa
        self.assertEqual(vista["equipo"], "R1")
        self.assertEqual(vista["argumentos"]["interfaz"], "ether4")
        self.assertEqual(vista["argumentos"]["comentario"], COMENTARIO_DEMO)
        self.assertEqual(vista["argumentos"]["equipo"], "r1")
        self.assertIn("comment", vista["efecto"])
        self.assertIsNone(vista["comentario_anterior"])
        self.assertIn("no abre conexión", vista["nota"])

    def test_los_pasos_mencionan_confirmacion_y_verificacion(self):
        plan = self._plan()
        texto = " ".join(plan.pasos)
        self.assertIn("confirmacion=True", texto)
        self.assertIn("Releer", texto)

    def test_sw1_y_ether3_siguen_rechazados_al_planificar(self):
        casos = (
            {"equipo": "sw1"},
            {"interfaz": "ether3"},
        )
        for cambio in casos:
            with self.subTest(cambio=cambio):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    self._plan(**cambio)
                self.assertEqual(contexto.exception.tipo, "interfaz_no_autorizada")

    def test_herramienta_inexistente_no_produce_plan(self):
        with self.assertRaises(ErrorHerramienta) as contexto:
            planificar(self.ajustes, "reiniciar_equipo", {"equipo": "r1"})
        self.assertEqual(contexto.exception.tipo, "herramienta_no_autorizada")

    def test_plan_y_vista_previa_son_serializables(self):
        plan = self._plan()
        datos = plan.a_dict()
        json.dumps(datos, ensure_ascii=False)

        self.assertEqual(
            list(datos), ["accion", "pasos", "requiere_autorizacion", "vista_previa"]
        )
        self.assertEqual(datos["accion"]["herramienta"], ACTUALIZAR_COMENTARIO)
        self.assertEqual(datos["accion"]["riesgo"], "escritura")
        self.assertEqual(datos["vista_previa"]["argumentos"]["comentario"], COMENTARIO_DEMO)
        self.assertTrue(datos["vista_previa"]["requiere_aprobacion"])


class PruebaCancelacion(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def test_cancelar_no_ejecuta_ni_conecta(self):
        plan = planificar(
            self.ajustes,
            ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": COMENTARIO_DEMO},
        )
        funcion = FuncionFalsa()
        with patch("herramientas.comun.ConnectHandler", side_effect=SIN_CONEXION):
            resultado = cancelar(plan)

        self.assertEqual(funcion.llamadas, [])
        self.assertIsInstance(resultado, Resultado)
        self.assertFalse(resultado.ok)
        self.assertFalse(resultado.ejecutada)
        self.assertFalse(resultado.aprobada)
        self.assertIsNone(resultado.salida)
        self.assertEqual(resultado.error["tipo"], "cancelado")
        self.assertEqual(resultado.herramienta, ACTUALIZAR_COMENTARIO)
        self.assertEqual(resultado.riesgo, "escritura")
        json.dumps(resultado.a_dict(), ensure_ascii=False)

    def test_cancelar_acepta_un_motivo(self):
        plan = planificar(self.ajustes, CONSULTAR_INTERFACES, {"equipo": "r1"})
        resultado = cancelar(plan, "El operador prefirió revisarlo después")
        self.assertEqual(resultado.error["tipo"], "cancelado")
        self.assertEqual(resultado.error["mensaje"], "El operador prefirió revisarlo después")


if __name__ == "__main__":
    unittest.main()
