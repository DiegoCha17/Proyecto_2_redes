"""Ciclo de lectura con proveedor y equipos simulados; no abre conexiones."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from agente.catalogo import (ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES,
                             Catalogo, Herramienta, catalogo_por_defecto)
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import LlamadaHerramienta, RespuestaModelo
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA


def llamada(equipo, identificador):
    return RespuestaModelo(llamadas=(LlamadaHerramienta(
        CONSULTAR_INTERFACES, {"equipo": equipo}, identificador),))


class ProveedorSimulado:
    def __init__(self, respuestas):
        self.respuestas = iter(respuestas)
        self.peticiones = []
        self.esquemas = []

    def responder_historial(self, mensajes, herramientas):
        self.peticiones.append(json.loads(json.dumps(mensajes)))
        self.esquemas.append(json.loads(json.dumps(herramientas)))
        return next(self.respuestas)


class PruebaCicloLectura(unittest.TestCase):
    def setUp(self):
        entorno = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        entorno.start()
        self.addCleanup(entorno.stop)
        with patch("config.ajustes.load_dotenv"):
            self.ajustes = cargar_ajustes()
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.ejecuciones = []
        self.escrituras = []

    def construir(self, respuestas, salida=None, limite=6, confirmar=None):
        salida = salida or {"ok": True, "interfaces": [
            {"nombre": "ether4", "estado": "running", "ip": "10.60.30.202/24",
             "comentario": ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"]}], "error": None}

        def leer(**argumentos):
            self.ejecuciones.append(argumentos["equipo"])
            return salida

        def escribir(**argumentos):
            self.escrituras.append(argumentos)
            return {"ok": True, "changed": False, "verificacion": {"ok": True, "coincide": True}}

        real = catalogo_por_defecto()
        catalogo = Catalogo([
            Herramienta(real.obtener(CONSULTAR_INTERFACES).definicion, leer),
            Herramienta(real.obtener(ACTUALIZAR_COMENTARIO).definicion, escribir),
        ])
        proveedor = ProveedorSimulado(respuestas)
        cerebro = Cerebro(replace(self.ajustes, max_herramientas_por_solicitud=limite),
                          proveedor=proveedor, catalogo=catalogo, salida=lambda _: None,
                          confirmar=confirmar or (lambda _: False),
                          registro=RegistroSesion(directorio=self.temporal.name))
        return cerebro, proveedor

    def test_dos_consultas_y_respuesta_final(self):
        cerebro, proveedor = self.construir([
            llamada("r1", "id-1"), llamada("sw1", "id-2"),
            RespuestaModelo(texto="Ambos equipos tienen interfaces activas.")])
        respuesta = cerebro.procesar("Consulta R1 y SW1")
        self.assertTrue(respuesta.ok)
        self.assertEqual(self.ejecuciones, ["r1", "sw1"])
        self.assertIn("Ambos equipos", respuesta.texto)
        historial = proveedor.peticiones[2]
        tools = [mensaje for mensaje in historial if mensaje["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["id-1", "id-2"])
        self.assertEqual(json.loads(tools[0]["content"]), {
            "ok": True, "equipo": "r1", "interfaces": [{"nombre": "ether4", "estado": "running"}]})
        serializado = json.dumps(historial)
        self.assertNotIn(ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"], serializado)
        self.assertNotIn("10.60.30.202", serializado)
        self.assertNotIn("comentario", tools[0]["content"])
        self.assertEqual([e["function"]["name"] for e in proveedor.esquemas[1]],
                         [CONSULTAR_INTERFACES, ACTUALIZAR_COMENTARIO])

    def test_operador_conserva_detalle_y_modelo_solo_recibe_proyeccion(self):
        cerebro, proveedor = self.construir([llamada("r1", "id"), RespuestaModelo(texto="Listo")])
        vistas = []
        cerebro._salida = vistas.append
        self.assertTrue(cerebro.procesar("Consulta R1").ok)
        self.assertTrue(any("10.60.30.202/24" in linea for linea in vistas))
        self.assertNotIn("10.60.30.202", json.dumps(proveedor.peticiones))

    def test_enumera_todas_las_interfaces_down_observadas(self):
        salida = {"ok": True, "interfaces": [
            {"nombre": "ether1", "estado": "running"},
            *[{"nombre": f"ether{i}", "estado": "down"} for i in range(4, 9)],
            {"nombre": "lo", "estado": "running"},
            {"nombre": "ether9", "estado": "disabled"},
        ], "error": None}
        cerebro, _ = self.construir([llamada("r1", "id"),
            RespuestaModelo(texto="La interfaz desconectada es ether4.")], salida=salida)
        respuesta = cerebro.procesar("Consulta las interfaces de R1 y dime cuáles están desconectadas.")
        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.texto, "En R1, las interfaces desconectadas son ether4, ether5, ether6, ether7 y ether8. Todas aparecen con estado down.")
        self.assertNotIn("ether9", respuesta.texto)

    def test_no_presenta_enum_completa_si_se_filtraron_datos(self):
        salida = {"ok": True, "interfaces": [
            {"nombre": "ether4", "estado": "down"},
            {"nombre": "nombre no permitido", "estado": "down"}], "error": None}
        cerebro, _ = self.construir([llamada("r1", "id"),
            RespuestaModelo(texto="Respuesta parcial.")], salida=salida)
        respuesta = cerebro.procesar("¿Cuáles interfaces de R1 están desconectadas?")
        self.assertEqual(respuesta.texto, "Respuesta parcial.")

    def test_no_atribuye_estados_a_equipo_no_consultado(self):
        cerebro, _ = self.construir([llamada("sw1", "id"),
            RespuestaModelo(texto="Solo se consultó SW1.")])
        respuesta = cerebro.procesar("¿Cuáles interfaces de R1 están desconectadas?")
        self.assertEqual(respuesta.texto, "Solo se consultó SW1.")

    def test_consulta_de_varios_estados_conserva_respuesta_del_modelo(self):
        salida = {"ok": True, "interfaces": [
            {"nombre": "ether1", "estado": "running"},
            {"nombre": "ether4", "estado": "down"}], "error": None}
        cerebro, _ = self.construir([llamada("r1", "id"),
            RespuestaModelo(texto="Activa ether1; desconectada ether4.")], salida=salida)
        respuesta = cerebro.procesar("Enumera las interfaces activas y desconectadas de R1")
        self.assertEqual(respuesta.texto, "Activa ether1; desconectada ether4.")

    def test_campo_nombre_que_contiene_secreto_no_se_envia(self):
        secreto = ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"]
        salida = {"ok": True, "interfaces": [
            {"nombre": secreto, "estado": "running"},
            {"nombre": "ether4", "estado": "down", "password": secreto}], "error": None}
        cerebro, proveedor = self.construir([llamada("r1", "id"), RespuestaModelo(texto="Listo")], salida)
        self.assertTrue(cerebro.procesar("Consulta R1").ok)
        self.assertNotIn(secreto, json.dumps(proveedor.peticiones))
        contenido = json.loads(proveedor.peticiones[1][-1]["content"])
        self.assertEqual(contenido["interfaces"], [{"nombre": "ether4", "estado": "down"}])

    def test_limite_es_configurable_e_independiente_de_pasos(self):
        with patch.dict(os.environ, {"NETAGENT_MAX_HERRAMIENTAS": "3", "NETAGENT_MAX_PASOS": "9"}):
            ajustes = cargar_ajustes()
        self.assertEqual(ajustes.max_herramientas_por_solicitud, 3)
        self.assertEqual(ajustes.max_pasos, 9)

    def test_error_se_observa_sin_filtrar_detalles(self):
        salida = {"ok": False, "error": {"tipo": "interno", "mensaje": "traza privada"}}
        cerebro, proveedor = self.construir([llamada("r1", "id"), RespuestaModelo(texto="Hecho")], salida)
        respuesta = cerebro.procesar("Consulta R1")
        self.assertFalse(respuesta.ok)
        self.assertEqual(json.loads(proveedor.peticiones[1][-1]["content"]),
                         {"ok": False, "equipo": "r1", "error": "lectura_no_disponible"})
        self.assertNotIn("traza privada", json.dumps(proveedor.peticiones))

    def test_maximo_y_ciclo_infinito(self):
        cerebro, proveedor = self.construir([llamada("r1", f"id-{i}") for i in range(7)])
        respuesta = cerebro.procesar("Consulta R1")
        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "limite_herramientas")
        self.assertEqual(len(self.ejecuciones), 6)
        self.assertEqual(len(proveedor.peticiones), 7)

    def test_permite_respuesta_final_al_agotar_limite(self):
        cerebro, _ = self.construir([llamada("r1", "id"), RespuestaModelo(texto="Terminado")], limite=1)
        self.assertTrue(cerebro.procesar("Consulta R1").ok)
        self.assertEqual(len(self.ejecuciones), 1)

    def test_id_repetido_se_rechaza(self):
        cerebro, _ = self.construir([llamada("r1", "id"), llamada("sw1", "id")])
        respuesta = cerebro.procesar("Consulta ambos")
        self.assertEqual(respuesta.error["tipo"], "respuesta_invalida")
        self.assertEqual(self.ejecuciones, ["r1"])

    def test_no_se_ejecuta_escritura_posterior_a_lectura(self):
        escritura = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": "demo"}, "id2"),))
        cerebro, _ = self.construir([llamada("r1", "id1"), escritura], confirmar=lambda _: True)
        respuesta = cerebro.procesar("Consulta R1")
        # El comentario del doble contiene un secreto: el diagnóstico no se
        # entrega al modelo y la escritura debe fallar cerrada.
        self.assertEqual(respuesta.error["tipo"], "diagnostico_fallido")
        self.assertEqual(self.escrituras, [])

    def test_escritura_inicial_mantiene_aprobacion(self):
        escritura = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": "demo"}, "id"),))
        segunda = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r1", "interfaz": "ether4", "comentario": "demo"}, "id2"),))
        salida = {"ok": True, "interfaces": [{"nombre": "ether4", "estado": "running", "comentario": "anterior"}], "error": None}
        cerebro, _ = self.construir([escritura, segunda], salida=salida)
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.ejecuciones, ["r1"])

    def test_herramienta_no_autorizada_no_se_ejecuta(self):
        peticion = RespuestaModelo(llamadas=(LlamadaHerramienta("shell", {"equipo": "r1"}, "id"),))
        cerebro, _ = self.construir([peticion])
        respuesta = cerebro.procesar("Ejecuta comandos")
        self.assertEqual(respuesta.error["tipo"], "herramienta_no_autorizada")
        self.assertEqual(self.ejecuciones, [])


if __name__ == "__main__":
    unittest.main()
