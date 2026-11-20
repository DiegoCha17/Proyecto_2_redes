"""Adaptador de solo lectura para la consulta Ansible de CU-02."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from herramientas.comun import ErrorHerramienta


RAIZ = Path(__file__).resolve().parents[1]


def consultar_bloqueo(equipo: str, ajustes: Any = None) -> dict[str, Any]:
    if equipo != "r1":
        raise ErrorHerramienta("dispositivo_no_autorizado", "La consulta de firewall solo permite R1")
    entorno = os.environ.copy()
    entorno["ANSIBLE_CONFIG"] = str(RAIZ / "ansible/ansible.cfg")
    entorno["ANSIBLE_STDOUT_CALLBACK"] = "ansible.posix.json"
    entorno["ANSIBLE_NOCOLOR"] = "1"
    try:
        proceso = subprocess.run(["ansible-playbook", "-i", str(RAIZ / "ansible/inventario.ini"),
                                  "--limit", "r1", str(RAIZ / "ansible/consultar_bloqueo.yml")],
                                 cwd=RAIZ, env=entorno, capture_output=True, text=True,
                                 timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ErrorHerramienta("consulta_ansible_fallida", "No se pudo completar la consulta Ansible") from exc
    if proceso.returncode != 0:
        raise ErrorHerramienta("consulta_ansible_fallida", "La consulta Ansible falló")
    try:
        documento = json.loads(proceso.stdout)
        if not isinstance(documento, dict) or documento.get("stats", {}).get("r1", {}).get("failures") != 0:
            raise ValueError("estado inválido")
        if documento["stats"]["r1"].get("unreachable") != 0:
            raise ValueError("equipo no accesible")
        resultados = [task["hosts"]["r1"]["netagent_resultado"]
                      for play in documento["plays"] for task in play["tasks"]
                      if task.get("task", {}).get("name") == "Resultado JSON de NetAgent-IA"
                      and "netagent_resultado" in task.get("hosts", {}).get("r1", {})]
        if len(resultados) != 1:
            raise ValueError("resultado ausente o duplicado")
        dato = resultados[0]
        campos = ("bloqueo_configurado", "regla_administrada_existente", "reglas_duplicadas", "segura_para_modificar")
        if (not isinstance(dato, dict) or dato.get("ok") is not True or dato.get("changed") is not False
                or dato.get("equipo") != "R1" or dato.get("origen") != "192.168.10.50"
                or dato.get("destino") != "172.16.100.10" or "bloqueo_efectivo" not in dato
                or dato["bloqueo_efectivo"] is not None
                or any(type(dato.get(k)) is not bool for k in campos)):
            raise ValueError("resultado inválido")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ErrorHerramienta("respuesta_ansible_invalida", "Respuesta Ansible incompleta o inválida") from exc
    return {"ok": True, "equipo": "R1", "origen": "192.168.10.50", "destino": "172.16.100.10",
            **{k: dato[k] for k in campos}, "bloqueo_efectivo": None, "changed": False, "error": None}
