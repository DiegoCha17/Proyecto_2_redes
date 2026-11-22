"""Pruebas del adaptador de escritura Ansible CU-02, sin red real."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agente.consulta_ansible import gestionar_bloqueo
from config.ajustes import cargar_ajustes
from herramientas.comun import ErrorHerramienta
from tests.test_comun import ENTORNO_PRUEBA


def documento(ip="192.168.10.60", bloqueada=True, changed=True, verified=True):
    dato = {
        "ok": True,
        "equipo": "R1",
        "ip": ip,
        "bloqueada": bloqueada,
        "changed": changed,
        "verified": verified,
        "error": None,
    }
    return {
        "stats": {"r1": {"failures": 0, "unreachable": 0}},
        "plays": [{"tasks": [{
            "task": {"name": "Resultado JSON de NetAgent-IA"},
            "hosts": {"r1": {"netagent_resultado": dato}},
        }]}],
    }


def ajustes():
    with patch.dict("os.environ", ENTORNO_PRUEBA, clear=True):
        return cargar_ajustes()


def test_parsea_y_valida_contrato_de_gestion():
    proceso = SimpleNamespace(returncode=0, stdout=json.dumps(documento()))
    with patch("agente.consulta_ansible.subprocess.run", return_value=proceso) as ejecutar:
        resultado = gestionar_bloqueo(
            "r1", "192.168.10.60", "bloquear", confirmacion=True, ajustes=ajustes()
        )

    assert resultado == {
        "ok": True,
        "equipo": "R1",
        "ip": "192.168.10.60",
        "bloqueada": True,
        "changed": True,
        "verified": True,
        "error": None,
    }
    args, kwargs = ejecutar.call_args
    assert args[0][0] == "ansible-playbook"
    assert "--extra-vars" in args[0]
    extra = json.loads(args[0][args[0].index("--extra-vars") + 1])
    assert extra == {"ip": "192.168.10.60", "accion": "bloquear"}
    assert args[0][-1].endswith("ansible/gestionar_bloqueo.yml")
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 120


def test_no_ejecuta_sin_aprobacion():
    with patch("agente.consulta_ansible.subprocess.run") as ejecutar:
        with pytest.raises(ErrorHerramienta) as error:
            gestionar_bloqueo(
                "r1", "192.168.10.60", "bloquear", ajustes=ajustes()
            )
    assert error.value.tipo == "aprobacion_requerida"
    ejecutar.assert_not_called()


def test_rechaza_ip_fuera_de_lan_sin_ejecutar_ansible():
    with patch("agente.consulta_ansible.subprocess.run") as ejecutar:
        with pytest.raises(ErrorHerramienta) as error:
            gestionar_bloqueo(
                "r1", "172.16.100.10", "bloquear", confirmacion=True, ajustes=ajustes()
            )
    assert error.value.tipo == "ip_no_autorizada"
    ejecutar.assert_not_called()


def test_idempotencia_de_bloquear_se_expone_en_el_contrato():
    proceso = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(documento(bloqueada=True, changed=False, verified=True)),
    )
    with patch("agente.consulta_ansible.subprocess.run", return_value=proceso):
        resultado = gestionar_bloqueo(
            "r1", "192.168.10.60", "bloquear", confirmacion=True, ajustes=ajustes()
        )
    assert resultado["bloqueada"] is True
    assert resultado["changed"] is False
    assert resultado["verified"] is True


def test_rechaza_contrato_con_campos_extra_o_verificacion_falsa():
    dato = documento()
    dato["plays"][0]["tasks"][0]["hosts"]["r1"]["netagent_resultado"]["extra"] = "no"
    proceso = SimpleNamespace(returncode=0, stdout=json.dumps(dato))
    with patch("agente.consulta_ansible.subprocess.run", return_value=proceso):
        with pytest.raises(ErrorHerramienta) as error:
            gestionar_bloqueo(
                "r1", "192.168.10.60", "bloquear", confirmacion=True, ajustes=ajustes()
            )
    assert error.value.tipo == "respuesta_ansible_invalida"

