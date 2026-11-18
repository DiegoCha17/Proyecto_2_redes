"""Pruebas del catálogo cerrado de herramientas del agente (sin red)."""

from __future__ import annotations

import unittest

from agente.catalogo import (
    ACTUALIZAR_COMENTARIO,
    CONSULTAR_INTERFACES,
    RIESGO_ESCRITURA,
    RIESGO_LECTURA,
    Catalogo,
    DefinicionHerramienta,
    Herramienta,
    catalogo_por_defecto,
)
from herramientas.comun import ErrorHerramienta


class FuncionFalsa:
    """Doble de una herramienta: registra cómo la llamaron."""

    def __init__(self, salida=None, error=None) -> None:
        self.salida = {"ok": True, "error": None} if salida is None else salida
        self.error = error
        self.llamadas: list[dict] = []

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.salida


def definicion_falsa(
    nombre: str = "herramienta_falsa", riesgo: str = RIESGO_LECTURA
) -> DefinicionHerramienta:
    return DefinicionHerramienta(
        nombre=nombre,
        descripcion="Herramienta de prueba",
        riesgo=riesgo,
        parametros={"equipo": {"type": "string"}},
        obligatorios=("equipo",),
    )


class PruebaCatalogoPorDefecto(unittest.TestCase):
    def test_solo_declara_las_herramientas_autorizadas(self):
        catalogo = catalogo_por_defecto()
        self.assertEqual(catalogo.nombres(), (CONSULTAR_INTERFACES, ACTUALIZAR_COMENTARIO))
        self.assertEqual(catalogo.obtener(CONSULTAR_INTERFACES).riesgo, RIESGO_LECTURA)
        self.assertEqual(
            catalogo.obtener(ACTUALIZAR_COMENTARIO).riesgo, RIESGO_ESCRITURA
        )

    def test_esquemas_sirven_para_function_calling(self):
        catalogo = catalogo_por_defecto()
        esquemas = catalogo.esquemas()
        self.assertEqual([esquema["function"]["name"] for esquema in esquemas],
                         list(catalogo.nombres()))
        for esquema in esquemas:
            with self.subTest(herramienta=esquema["function"]["name"]):
                self.assertEqual(esquema["type"], "function")
                parametros = esquema["function"]["parameters"]
                self.assertEqual(parametros["type"], "object")
                self.assertFalse(parametros["additionalProperties"])
                for obligatorio in parametros["required"]:
                    self.assertIn(obligatorio, parametros["properties"])
                self.assertEqual(
                    set(parametros["required"]), set(parametros["properties"])
                )

    def test_la_escritura_solo_admite_equipo_interfaz_y_comentario(self):
        parametros = (
            catalogo_por_defecto().obtener(ACTUALIZAR_COMENTARIO).esquema()["function"]
        )["parameters"]
        self.assertEqual(set(parametros["properties"]), {"equipo", "interfaz", "comentario"})
        self.assertNotIn("confirmacion", parametros["properties"])


class PruebaRechazos(unittest.TestCase):
    def test_herramienta_inexistente_se_rechaza(self):
        catalogo = catalogo_por_defecto()
        for nombre in ("reiniciar_equipo", "ejecutar_comando", "", "consultar_interfaces "):
            with self.subTest(nombre=nombre):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    catalogo.obtener(nombre)
                self.assertEqual(contexto.exception.tipo, "herramienta_no_autorizada")
                self.assertIn(CONSULTAR_INTERFACES, contexto.exception.mensaje)

    def test_invocar_una_herramienta_inexistente_no_ejecuta_nada(self):
        funcion = FuncionFalsa()
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        with self.assertRaises(ErrorHerramienta) as contexto:
            catalogo.invocar("system_reboot", {"equipo": "r1"})
        self.assertEqual(contexto.exception.tipo, "herramienta_no_autorizada")
        self.assertEqual(funcion.llamadas, [])

    def test_argumentos_desconocidos_se_rechazan(self):
        funcion = FuncionFalsa()
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        with self.assertRaises(ErrorHerramienta) as contexto:
            catalogo.invocar(
                "herramienta_falsa",
                {"equipo": "r1", "comando": "/system reboot"},
            )
        self.assertEqual(contexto.exception.tipo, "argumentos_invalidos")
        self.assertIn("comando", contexto.exception.mensaje)
        self.assertEqual(funcion.llamadas, [])

    def test_argumentos_obligatorios_o_de_tipo_invalido_se_rechazan(self):
        funcion = FuncionFalsa()
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        casos = ({}, {"equipo": 1}, {"equipo": None}, {"equipo": ["r1"]}, "equipo=r1")
        for argumentos in casos:
            with self.subTest(argumentos=argumentos):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    catalogo.invocar("herramienta_falsa", argumentos)
                self.assertEqual(contexto.exception.tipo, "argumentos_invalidos")
        self.assertEqual(funcion.llamadas, [])

    def test_no_se_puede_colar_confirmacion_como_argumento(self):
        funcion = FuncionFalsa()
        catalogo = Catalogo(
            [
                Herramienta(
                    definicion=definicion_falsa(riesgo=RIESGO_ESCRITURA),
                    funcion=funcion,
                )
            ]
        )
        with self.assertRaises(ErrorHerramienta) as contexto:
            catalogo.invocar(
                "herramienta_falsa", {"equipo": "r1", "confirmacion": "true"}
            )
        self.assertEqual(contexto.exception.tipo, "argumentos_invalidos")
        self.assertEqual(funcion.llamadas, [])

    def test_definicion_incoherente_falla_al_declararse(self):
        with self.assertRaises(ValueError):
            DefinicionHerramienta(
                nombre="rara",
                descripcion="riesgo inventado",
                riesgo="total",
                parametros={"equipo": {"type": "string"}},
                obligatorios=("equipo",),
            )
        with self.assertRaises(ValueError):
            DefinicionHerramienta(
                nombre="rara",
                descripcion="obligatorio sin parámetro",
                riesgo=RIESGO_LECTURA,
                parametros={"equipo": {"type": "string"}},
                obligatorios=("interfaz",),
            )

    def test_catalogo_vacio_o_duplicado_se_rechaza(self):
        with self.assertRaises(ValueError):
            Catalogo([])
        with self.assertRaises(ValueError):
            Catalogo(
                [
                    Herramienta(definicion=definicion_falsa(), funcion=FuncionFalsa()),
                    Herramienta(definicion=definicion_falsa(), funcion=FuncionFalsa()),
                ]
            )


class PruebaInvocacion(unittest.TestCase):
    def test_pasa_los_argumentos_validados_y_devuelve_la_salida(self):
        salida = {"ok": True, "equipo": "R1", "interfaces": [], "changed": False, "error": None}
        funcion = FuncionFalsa(salida=salida)
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        resultado = catalogo.invocar("herramienta_falsa", {"equipo": "r1"})
        self.assertEqual(resultado, salida)
        self.assertEqual(funcion.llamadas, [{"equipo": "r1"}])

    def test_las_opciones_de_invocacion_llegan_a_la_funcion(self):
        funcion = FuncionFalsa()
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        catalogo.invocar("herramienta_falsa", {"equipo": "r1"}, confirmacion=True)
        self.assertEqual(funcion.llamadas, [{"equipo": "r1", "confirmacion": True}])

    def test_error_tipado_de_la_herramienta_se_propaga(self):
        funcion = FuncionFalsa(error=ErrorHerramienta("conexion", "sin ruta"))
        catalogo = Catalogo(
            [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
        )
        with self.assertRaises(ErrorHerramienta) as contexto:
            catalogo.invocar("herramienta_falsa", {"equipo": "r1"})
        self.assertEqual(contexto.exception.tipo, "conexion")

    def test_excepcion_inesperada_o_salida_no_estructurada_es_error_tipado(self):
        casos = (
            FuncionFalsa(error=RuntimeError("fallo simulado")),
            FuncionFalsa(salida="texto libre"),
            FuncionFalsa(salida=[]),
        )
        for funcion in casos:
            with self.subTest(funcion=funcion):
                catalogo = Catalogo(
                    [Herramienta(definicion=definicion_falsa(), funcion=funcion)]
                )
                with self.assertRaises(ErrorHerramienta) as contexto:
                    catalogo.invocar("herramienta_falsa", {"equipo": "r1"})
                self.assertEqual(contexto.exception.tipo, "interno")
                self.assertNotIn("Traceback", contexto.exception.mensaje)


if __name__ == "__main__":
    unittest.main()
