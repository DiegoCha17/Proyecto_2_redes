"""Pruebas de herramientas/estado_interfaces.py con salidas RouterOS simuladas."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from netmiko import NetmikoTimeoutException

from config.ajustes import cargar_ajustes
from herramientas.comun import (
    COMANDO_DIRECCIONES,
    COMANDO_INTERFACES,
    construir_respuesta,
    error_json,
)
from herramientas.estado_interfaces import (
    dividir_bloques,
    dividir_registros,
    main,
    parsear_direcciones,
    parsear_interfaces,
    consultar,
)
from tests.test_comun import ENTORNO_PRUEBA, ConexionFalsa

# --- Salidas simuladas de RouterOS ------------------------------------------
# RouterOS 7: banderas R/X y propiedades en una sola línea (Netmiko fija 511).
SALIDA_INTERFACES_R1 = """\
Flags: D - dynamic; X - disabled, R - running; S - slave; P - passthrough

 0  R name="ether1" default-name="ether1" type="ether" mtu=1500 actual-mtu=1500 l2mtu=1598 max-l2mtu=1598 mac-address=AA:BB:CC:00:00:01 comment="WAN simulada"
 1  R name="ether2" default-name="ether2" type="ether" mtu=1500 actual-mtu=1500 l2mtu=1598 max-l2mtu=1598 mac-address=AA:BB:CC:00:00:02 comment="LAN R1"
 2  R name="bridge" type="bridge" mtu=auto actual-mtu=1500 l2mtu=1598 mac-address=AA:BB:CC:00:00:03 comment="Bridge LAN"
 3 X  name="ether5" default-name="ether5" type="ether" mtu=1500 actual-mtu=1500 l2mtu=1598 max-l2mtu=1598 mac-address=AA:BB:CC:00:00:05 disabled=yes comment="Reservada para pruebas"
 4  R name="ether4" default-name="ether4" type="ether" mtu=1500 actual-mtu=1500 l2mtu=1598 max-l2mtu=1598 running=yes comment=""
"""

SALIDA_DIRECCIONES_R1 = """\
Flags: X - disabled, I - invalid, D - dynamic

 0   ;;; LAN
     address=192.168.10.1/24 network=192.168.10.0 interface=bridge actual-interface=bridge invalid=no dynamic=no disabled=no
 1 D address=172.16.100.1/24 network=172.16.100.0 interface=ether1 actual-interface=ether1 dynamic=yes disabled=no
 2   address=10.60.30.202/24 network=10.60.30.0 interface=bridge actual-interface=bridge invalid=no dynamic=no disabled=no
 3 X address=10.99.99.1/24 network=10.99.99.0 interface=ether5 actual-interface=ether5 invalid=no dynamic=no disabled=yes
 4   address=192.168.10.1/24 network=192.168.10.0 interface=bridge actual-interface=bridge invalid=no dynamic=no disabled=no
"""

# RouterOS 6: columna de banderas vacía y estado en la propiedad running.
SALIDA_INTERFACES_SW1 = """\
Flags: D - dynamic, X - disabled, R - running

 0    name="ether1" type="ether" mtu=1500 running=yes comment="uplink a R1"
 1    name="ether2" type="ether" mtu=1500 running=yes comment="PC1"
 2    name="ether3" type="ether" mtu=1500 running=yes comment="PC2"
 3    name="ether4" type="ether" mtu=1500 comment="reserva"
"""

# RouterOS 7 con el comentario en la línea ``;;;`` que imprime ``print detail``
# (no aparece como ``comment=``). Es la forma confirmada en R1 tras aplicar el
# comentario: la propiedad existe pero la terminal solo muestra la línea ``;;;``.
SALIDA_DETALLE_R1 = """\
Flags: D - dynamic; X - disabled, R - running; S - slave; P - passthrough

 0  R ;;; P2-NetAgent-IA-Demo
      name="ether4" default-name="ether4" type="ether" mtu=1500 actual-mtu=1500
      l2mtu=1598 max-l2mtu=1598 mac-address=AA:BB:CC:00:00:04
 1  R name="ether1" default-name="ether1" type="ether" mtu=1500 actual-mtu=1500
      l2mtu=1598 mac-address=AA:BB:CC:00:00:01
 2  R ;;; LAN  con  espacios
      name="ether2" default-name="ether2" type="ether" mtu=1500 actual-mtu=1500
 3 X  ;;; Reservada para pruebas
      name="ether5" default-name="ether5" type="ether" mtu=1500 disabled=yes
"""

INTERFACES_R1_ESPERADAS = [
    {"nombre": "ether1", "estado": "running", "ip": "172.16.100.1/24", "comentario": "WAN simulada"},
    {"nombre": "ether2", "estado": "running", "ip": "", "comentario": "LAN R1"},
    {"nombre": "bridge", "estado": "running", "ip": "192.168.10.1/24, 10.60.30.202/24", "comentario": "Bridge LAN"},
    {"nombre": "ether5", "estado": "disabled", "ip": "", "comentario": "Reservada para pruebas"},
    {"nombre": "ether4", "estado": "running", "ip": "", "comentario": ""},
]


class PruebaParserRouterOS(unittest.TestCase):
    def test_divide_un_registro_por_interfaz(self):
        registros = dividir_registros(SALIDA_INTERFACES_R1)
        self.assertEqual(len(registros), 5)
        self.assertTrue(registros[0].startswith("0"))

    def test_division_une_comentario_y_continuacion(self):
        bloques = dividir_bloques(SALIDA_DIRECCIONES_R1)
        self.assertEqual(len(bloques), 5)
        self.assertEqual(bloques[0]["comentario"], "LAN")
        self.assertIn("address=192.168.10.1/24", " ".join(bloques[0]["lineas"]))

    def test_parsea_interfaces_routeros_7(self):
        self.assertEqual(parsear_interfaces(SALIDA_INTERFACES_R1), [
            {**interfaz, "ip": ""} for interfaz in INTERFACES_R1_ESPERADAS
        ])

    def test_parsea_interfaces_routeros_6_sin_banderas(self):
        interfaces = parsear_interfaces(SALIDA_INTERFACES_SW1)
        self.assertEqual(
            [(i["nombre"], i["estado"]) for i in interfaces],
            [
                ("ether1", "running"),
                ("ether2", "running"),
                ("ether3", "running"),
                ("ether4", "down"),
            ],
        )
        self.assertEqual(interfaces[1]["comentario"], "PC1")

    def test_parsea_direcciones_omite_deshabilitadas_y_duplicadas(self):
        self.assertEqual(
            parsear_direcciones(SALIDA_DIRECCIONES_R1),
            {
                "bridge": ["192.168.10.1/24", "10.60.30.202/24"],
                "ether1": ["172.16.100.1/24"],
            },
        )

    def test_salida_vacia_no_produce_interfaces(self):
        self.assertEqual(parsear_interfaces(""), [])
        self.assertEqual(parsear_direcciones("Flags: X - disabled\n"), {})


class PruebaComentariosRouterOS(unittest.TestCase):
    """El comentario de ``print detail`` llega como línea ``;;;``.

    Antes de la corrección solo se leía la propiedad ``comment=``, así que una
    entrada con el comentario ya aplicado se informaba como ``""`` y la
    verificación de actualizar_comentario fallaba.
    """

    def test_comentario_en_linea_propia(self):
        interfaces = parsear_interfaces(SALIDA_DETALLE_R1)
        self.assertEqual(interfaces[0]["nombre"], "ether4")
        self.assertEqual(interfaces[0]["comentario"], "P2-NetAgent-IA-Demo")

    def test_interfaz_sin_comentario_queda_vacia(self):
        interfaces = parsear_interfaces(SALIDA_DETALLE_R1)
        self.assertEqual(interfaces[1]["nombre"], "ether1")
        self.assertEqual(interfaces[1]["comentario"], "")

    def test_varias_interfaces_comentadas_y_sin_comentario(self):
        self.assertEqual(
            [(i["nombre"], i["comentario"]) for i in parsear_interfaces(SALIDA_DETALLE_R1)],
            [
                ("ether4", "P2-NetAgent-IA-Demo"),
                ("ether1", ""),
                ("ether2", "LAN  con  espacios"),
                ("ether5", "Reservada para pruebas"),
            ],
        )

    def test_comentario_con_espacios_conserva_los_internos(self):
        interfaces = parsear_interfaces(SALIDA_DETALLE_R1)
        self.assertEqual(interfaces[2]["comentario"], "LAN  con  espacios")

    def test_el_comentario_no_se_mezcla_con_la_entrada_siguiente(self):
        salida = """\
Flags: X - disabled, R - running

 0  R ;;; solo para ether1
      name="ether1" type="ether" running=yes
 1  R name="ether2" type="ether" running=yes
"""
        ether1, ether2 = parsear_interfaces(salida)
        self.assertEqual(ether1["comentario"], "solo para ether1")
        self.assertEqual(ether2["comentario"], "")

    def test_comentario_tras_las_propiedades_pertenece_a_su_entrada(self):
        salida = """\
Flags: X - disabled, R - running

 0  R name="ether1" type="ether" running=yes
      ;;; al final del registro 0
 1  R name="ether2" type="ether" running=yes
"""
        ether1, ether2 = parsear_interfaces(salida)
        self.assertEqual(ether1["comentario"], "al final del registro 0")
        self.assertEqual(ether2["comentario"], "")

    def test_formato_comment_igual_se_conserva(self):
        salida = """\
Flags: X - disabled, R - running

 0  R name="ether1" type="ether" running=yes comment="WAN simulada"
 1  R name="ether2" type="ether" running=yes comment=""
 2  R name="ether3" type="ether" running=yes comment="con espacios  internos"
"""
        self.assertEqual(
            [i["comentario"] for i in parsear_interfaces(salida)],
            ["WAN simulada", "", "con espacios  internos"],
        )

    def test_comment_igual_con_marca_parecida_no_se_parte(self):
        salida = (
            'Flags: X - disabled, R - running\n\n'
            ' 0  R name="ether1" type="ether" comment="a;;;b"\n'
        )
        self.assertEqual(parsear_interfaces(salida)[0]["comentario"], "a;;;b")

    def test_estado_y_banderas_siguen_funcionando_con_comentario(self):
        interfaces = parsear_interfaces(SALIDA_DETALLE_R1)
        self.assertEqual(
            [i["estado"] for i in interfaces],
            ["running", "running", "running", "disabled"],
        )

    def test_las_propiedades_no_arrastran_el_comentario(self):
        bloques = dividir_bloques(SALIDA_DETALLE_R1)
        self.assertEqual(len(bloques), 4)
        self.assertEqual(bloques[0]["lineas"][0], "0  R")
        self.assertFalse(any(";;;" in linea for linea in bloques[0]["lineas"]))


class PruebaConsulta(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def test_consulta_r1_devuelve_json_esperado(self):
        falsa = ConexionFalsa(
            {
                COMANDO_INTERFACES: SALIDA_INTERFACES_R1,
                COMANDO_DIRECCIONES: SALIDA_DIRECCIONES_R1,
            }
        )
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertEqual(
            resultado,
            {
                "ok": True,
                "equipo": "R1",
                "interfaces": INTERFACES_R1_ESPERADAS,
                "changed": False,
                "error": None,
            },
        )
        json.dumps(resultado, ensure_ascii=False)
        # Solo se ejecutaron comandos de lectura, en orden, y se cerró la sesión.
        self.assertEqual(falsa.comandos, [COMANDO_INTERFACES, COMANDO_DIRECCIONES])
        self.assertTrue(falsa.desconectada)

    def test_consulta_devuelve_el_comentario_de_la_linea_routeros(self):
        falsa = ConexionFalsa(
            {
                COMANDO_INTERFACES: SALIDA_DETALLE_R1,
                COMANDO_DIRECCIONES: SALIDA_DIRECCIONES_R1,
            }
        )
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertTrue(resultado["ok"])
        comentarios = {i["nombre"]: i["comentario"] for i in resultado["interfaces"]}
        self.assertEqual(comentarios["ether4"], "P2-NetAgent-IA-Demo")
        self.assertEqual(comentarios["ether1"], "")
        self.assertEqual(comentarios["ether2"], "LAN  con  espacios")
        self.assertEqual(comentarios["ether5"], "Reservada para pruebas")

    def test_consulta_sw1_usa_su_host_y_sus_credenciales(self):
        falsa = ConexionFalsa({COMANDO_INTERFACES: SALIDA_INTERFACES_SW1})
        with patch("herramientas.comun.ConnectHandler", return_value=falsa) as mock:
            resultado = consultar("sw1", ajustes=self.ajustes)

        parametros = mock.call_args.kwargs
        self.assertEqual(parametros["host"], "10.60.30.204")
        self.assertEqual(parametros["username"], ENTORNO_PRUEBA["NETAGENT_SW1_USUARIO"])
        self.assertEqual(parametros["password"], ENTORNO_PRUEBA["NETAGENT_SW1_CLAVE"])
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["equipo"], "SW1")
        self.assertEqual(len(resultado["interfaces"]), 4)

    def test_equipo_no_autorizado_no_abre_conexion(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            resultado = consultar("pc1", ajustes=self.ajustes)

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "dispositivo_no_autorizado")
        self.assertEqual(resultado["interfaces"], [])
        mock.assert_not_called()

    def test_error_de_conexion_no_inventa_interfaces(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=NetmikoTimeoutException("tiempo agotado"),
        ):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "tiempo_agotado")
        self.assertEqual(resultado["interfaces"], [])
        self.assertFalse(resultado["changed"])
        self.assertIn("GNS3", resultado["error"]["mensaje"])

    def test_respuesta_irreconocible_no_se_reporta_como_vacia(self):
        falsa = ConexionFalsa({COMANDO_INTERFACES: "Flags: X - disabled\n\n"})
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "respuesta_no_reconocida")
        self.assertEqual(resultado["interfaces"], [])

    def test_credenciales_faltantes_se_informan(self):
        with patch.dict(
            os.environ, {"NETAGENT_R1_USUARIO": "", "NETAGENT_R1_CLAVE": ""}
        ):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["error"]["tipo"], "configuracion")
        self.assertIn("NETAGENT_R1_USUARIO", resultado["error"]["mensaje"])

    def test_la_clave_nunca_aparece_en_la_respuesta(self):
        clave = ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"]
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=Exception(f"falla con {clave}"),
        ):
            resultado = consultar("r1", ajustes=self.ajustes)

        self.assertFalse(resultado["ok"])
        texto = json.dumps(resultado, ensure_ascii=False)
        self.assertNotIn(clave, texto)
        self.assertNotIn(clave, repr(resultado))


class PruebaCLI(unittest.TestCase):
    def test_cli_imprime_json_y_sale_cero(self):
        respuesta = construir_respuesta(
            "R1",
            [{"nombre": "ether1", "estado": "running", "ip": "", "comentario": ""}],
        )
        with patch("herramientas.estado_interfaces.consultar", return_value=respuesta):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                codigo = main(["r1"])

        self.assertEqual(codigo, 0)
        self.assertEqual(json.loads(buffer.getvalue()), respuesta)

    def test_cli_devuelve_uno_si_hay_error(self):
        respuesta = construir_respuesta("R1", error=error_json("conexion", "sin ruta"))
        with patch("herramientas.estado_interfaces.consultar", return_value=respuesta):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                codigo = main(["R1"])

        self.assertEqual(codigo, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
